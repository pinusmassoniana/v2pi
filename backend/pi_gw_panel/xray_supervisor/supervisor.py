import json
import subprocess
import threading
import time
from typing import TypedDict

from pi_gw_panel.xray_config.validate import scrub_output


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
    """

    READY_TIMEOUT = 2.0
    READY_STEP = 0.05
    STDERR_TAIL_CHARS = 8192

    def __init__(self, xray_bin: str, config_path: str, ready_check=None):
        self.xray_bin = xray_bin
        self.config_path = config_path
        self._ready_check = ready_check
        self._proc: subprocess.Popen | None = None
        self._want_running = False   # intent — distinguishes a deliberate stop from a crash
        self._lock = threading.RLock()
        self._last_exit_code: int | None = None   # last observed dead-at-boot returncode (diagnostics)
        self._stderr_tail = ""
        self._stderr_thread: threading.Thread | None = None
        self._stderr_lock = threading.Lock()
        self._redaction_config: dict = {}

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
            try:
                with open(self.config_path) as f:
                    config = json.load(f)
                self._redaction_config = config if isinstance(config, dict) else {}
            except (OSError, json.JSONDecodeError):
                self._redaction_config = {}
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

    def stop(self) -> None:
        with self._lock:
            self._want_running = False
            self._stop_child()

    def _stop_child(self) -> None:
        """Stop the current child without changing the desired running state."""
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if proc is not None:
            self._last_exit_code = proc.returncode
        self._join_stderr()
        self._proc = None

    def reload(self) -> bool:
        """Restart xray and report whether it came up. False = the new process died at boot or
        never became ready — the caller must roll the config back and reload, else all tunnelled
        traffic blackholes on a config that passed `-test` but can't actually run (port bound,
        cap drop, tproxy/nft state)."""
        with self._lock:
            self.stop()
            self.start()
            return self._wait_ready()

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
