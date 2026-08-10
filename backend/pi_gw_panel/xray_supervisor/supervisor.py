import json
import logging
import subprocess
import threading
import time
from typing import TypedDict

from pi_gw_panel.xray_config.validate import scrub_output

logger = logging.getLogger(__name__)


class SupervisorStatus(TypedDict):
    running: bool
    pid: int | None
    last_exit_code: int | None
    last_error: str
    stderr_tail: str


class XraySupervisor:
    """Owns the xray child process. reload = restart (xray has no live-reload signal).

    `ready_check` (optional, production-wired) is a `() -> bool` that reports whether the
    new process is accepting connections; reload() polls it briefly after start so callers
    don't route traffic into a not-yet-listening xray (audit B1). None (tests/dev) = no wait.

    All lifecycle transitions (start/stop/reload) take `_lock` so two concurrent request
    handlers can't race on `_proc` (one terminating a process another just spawned, or two
    Popens leaking the first — an orphan holding the tproxy port).

    `start_guard` (optional, production-wired) is a `() -> bool` asked, under `_lock`, whether
    the config on disk may be served AT ALL — the remote-access credential check. It lives here
    rather than at the callers because the caller that matters has no route to put it in: the
    liveness watchdog respawns a crashed xray on whatever the file says, so every guard placed in
    a request handler was one the watchdog walked around twenty seconds later. False means don't
    spawn, and is reported as a failed start.
    """

    READY_TIMEOUT = 2.0
    READY_STEP = 0.05
    STDERR_TAIL_CHARS = 8192
    TERM_TIMEOUT = 5.0     # SIGTERM → wait, before escalating to SIGKILL
    KILL_TIMEOUT = 5.0     # SIGKILL → wait, before giving up and reporting a failed stop

    def __init__(self, xray_bin: str, config_path: str, ready_check=None, start_guard=None):
        self.xray_bin = xray_bin
        self.config_path = config_path
        self._ready_check = ready_check
        self._start_guard = start_guard
        self._proc: subprocess.Popen | None = None
        self._want_running = False   # intent — distinguishes a deliberate stop from a crash
        self._lock = threading.RLock()
        self._last_exit_code: int | None = None   # last observed dead-at-boot returncode (diagnostics)
        self._stderr_tail = ""
        self._stderr_thread: threading.Thread | None = None
        self._stderr_lock = threading.Lock()
        self._redaction_config: dict = {}

    def set_start_guard(self, guard) -> None:
        """Install the `() -> bool` consulted before each spawn (see the class docstring). Set
        after construction because the guard needs the AppState the supervisor is part of."""
        with self._lock:
            self._start_guard = guard

    def start(self) -> None:
        with self._lock:
            self._want_running = True
            if self._proc and self._proc.poll() is None:
                return
            if self._proc is not None:
                self._last_exit_code = self._proc.returncode
                self._join_stderr()
                self._proc = None
            self._last_exit_code = None
            with self._stderr_lock:
                self._stderr_tail = ""
            # Asked here — inside the lock, after the "already running" exit, immediately before
            # the spawn — so no path reaches a fresh process without it and nothing can rewrite
            # the config between the answer and the exec. A refusal is a start that did not
            # happen: `_want_running` stays true so status() reads 'error' and the watchdog
            # keeps (backing-off) company, and the reason lands in the same tail the operator
            # already reads for "why is xray not up".
            if self._start_guard is not None and not self._start_guard():
                logger.error("refusing to start xray: the config on disk may not be served")
                with self._stderr_lock:
                    self._stderr_tail = ("refusing to start xray: the config on disk still "
                                         "grants access that has been revoked")
                return
            # Refresh the secret vocabulary used to scrub the stderr tail (exposed as
            # /api/status.last_error). On failure KEEP the last one we managed to read: the
            # config being unreadable is exactly when xray is loudest, and dropping to {}
            # switched redaction off wholesale — the uuid/keys of the config it last ran with
            # are still the ones its complaints quote.
            try:
                with open(self.config_path) as f:
                    config = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    "could not read %s for stderr redaction (%s) — keeping the previous "
                    "config's secrets redacted", self.config_path, exc)
            else:
                if isinstance(config, dict):
                    self._redaction_config = config
                else:
                    logger.warning(
                        "%s is not a JSON object — keeping the previous redaction vocabulary",
                        self.config_path)
            try:
                self._proc = subprocess.Popen(
                    [self.xray_bin, "-config", self.config_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    errors="replace",
                )
            except OSError as exc:
                self._proc = None
                with self._stderr_lock:
                    if isinstance(exc, FileNotFoundError):
                        self._stderr_tail = "xray executable not found"
                    else:
                        self._stderr_tail = f"unable to start xray: {exc.strerror or type(exc).__name__}"
                return
            self._stderr_thread = threading.Thread(
                target=self._capture_stderr, args=(self._proc,), daemon=True,
            )
            self._stderr_thread.start()

    def _capture_stderr(self, proc: subprocess.Popen) -> None:
        assert proc.stderr is not None
        while chunk := proc.stderr.read(4096):
            with self._stderr_lock:
                self._stderr_tail = (self._stderr_tail + chunk)[-self.STDERR_TAIL_CHARS:]

    def _join_stderr(self) -> None:
        thread = self._stderr_thread
        if thread is not None:
            thread.join(timeout=1)
        self._stderr_thread = None

    def stop(self) -> bool:
        """Stop xray and report whether it is actually down. False = the child outlived both
        signals; see `_stop_child`."""
        with self._lock:
            self._want_running = False
            return self._stop_child()

    def _stop_child(self) -> bool:
        """Stop the current child without changing the desired running state. Returns whether
        it is now provably gone.

        BOTH waits are bounded. The post-`kill()` one used to be `proc.wait()` with no timeout,
        and a child that SIGKILL cannot reap — stuck in an uninterruptible syscall, a wedged
        tproxy socket, a frozen mount — parked the caller there forever while holding `_lock`,
        `apply_lock` and (before the revocation was reordered) the store's single connection: one
        stuck process took the whole panel, not just the tunnel, and the revocation that was
        waiting on it could never commit. A stop that cannot be confirmed is a fact to report in
        bounded time, not something to wait out.

        `_proc` is KEPT when the stop fails. Clearing it would let the very next start spawn a
        second xray beside a first that is still holding the tproxy port — "we could not stop it"
        turned into "so start another one", which is the one response no caller wants. Keeping it
        means status() keeps telling the truth (it IS running) and `state()` reads 'working'
        rather than 'error', so the watchdog leaves it alone instead of restarting around it.
        """
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=self.TERM_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=self.KILL_TIMEOUT)
                except subprocess.TimeoutExpired:
                    logger.error(
                        "xray (pid %s) survived SIGKILL for %.0fs — reporting a failed stop",
                        getattr(proc, "pid", "?"), self.KILL_TIMEOUT)
                    return False
        if proc is not None:
            self._last_exit_code = proc.returncode
        self._join_stderr()
        self._proc = None
        return True

    def reload(self) -> bool:
        """Restart xray and report whether it came up. False = the new process died at boot or
        never became ready — the caller must roll the config back and reload, else all tunnelled
        traffic blackholes on a config that passed `-test` but can't actually run (port bound,
        cap drop, tproxy/nft state).

        This is stop→start unconditionally, NOT "restart if it was running": called while xray
        is deliberately stopped it STARTS it, and sets `_want_running`. A caller that must not
        resurrect a stopped process may not use this at all — not even after checking status(),
        because that check and this call are two separate lock holds. Use `reload_if_running`,
        which does both under one.

        A stop that cannot be confirmed ends it: False, and NOTHING is started. The old process
        is still alive and still holding the tproxy port, so starting a second one produces two
        xrays on one port rather than a reload, and the caller is told the apply failed — which
        is true, and which its rollback path already knows how to answer. "Could not stop it" may
        never become "so start it again"."""
        with self._lock:
            if not self.stop():
                return False
            self.start()
            return self._wait_ready()

    def reload_if_running(self) -> bool | None:
        """reload(), but only for a child that is running AT THE MOMENT OF THE RESTART.

        Three answers, and they are not interchangeable:
          * True  — it was running and came back up ready.
          * False — it was running and the reload did not confirm; a failed apply, exactly as a
                    False from reload() is, and the caller must react the same way. Includes the
                    child that could not be stopped at all: nothing was restarted, and a
                    revocation reading this must treat it as unapplied rather than retry a start.
          * None  — it was NOT running, so nothing was started and nothing was changed.

        This exists because "never start an xray the operator stopped" cannot be enforced from
        outside. Sampling status() and then calling reload() is two operations with a gap, and a
        child that exits inside that gap turns the reload's unconditional stop→start into a plain
        START of a process that was down — on whatever config happens to be on disk. For a
        revocation that is the whole leak in one line: the credential is served again by an xray
        nobody asked to run. The check therefore has to happen under the same `_lock` hold as the
        start it guards, which is possible only in here.

        `poll()` rather than `_want_running`: the question is whether a process is alive to be
        restarted, not whether we would like one to be. A crashed child that we still want
        running is exactly the case that must NOT be started by a revocation.
        """
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                return None
            return self.reload()

    def _wait_ready(self) -> bool:
        """Poll `ready_check` until it passes, the process dies, or the budget runs out.
        Returns True only when readiness was actually confirmed (or there is no ready_check to
        confirm against but the process is alive); False on death/timeout so callers can react."""
        proc = self._proc
        if proc is None:
            return False
        if self._ready_check is None:
            # no probe available — treat "alive right after start" as the best signal we have
            return proc.poll() is None
        deadline = time.monotonic() + self.READY_TIMEOUT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self._last_exit_code = proc.returncode   # died at boot
                self._join_stderr()
                return False
            try:
                if self._ready_check():
                    return True
            except Exception:
                pass
            time.sleep(self.READY_STEP)
        # A live but unready child is still a failed start. Leaving it behind can keep
        # ports/resources occupied while callers roll back and attempt recovery.
        self._stop_child()
        return False

    def status(self) -> SupervisorStatus:
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
            if self._proc is not None and not running:
                self._last_exit_code = self._proc.returncode
                self._join_stderr()
            with self._stderr_lock:
                last_error = scrub_output(self._stderr_tail, self._redaction_config)
            return {
                "running": running,
                "pid": self._proc.pid if running else None,
                "last_exit_code": self._last_exit_code,
                "last_error": last_error,
                "stderr_tail": last_error,
            }

    def state(self) -> str:
        """3-way state for the sidebar xray-core box: 'working' (running) |
        'error' (we wanted it running but it died) | 'stopped' (deliberate)."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return "working"
            return "error" if self._want_running else "stopped"
