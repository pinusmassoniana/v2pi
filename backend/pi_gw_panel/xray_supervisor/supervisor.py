import subprocess
import threading
import time
from typing import TypedDict


class SupervisorStatus(TypedDict):
    running: bool
    pid: int | None


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

    def __init__(self, xray_bin: str, config_path: str, ready_check=None):
        self.xray_bin = xray_bin
        self.config_path = config_path
        self._ready_check = ready_check
        self._proc: subprocess.Popen | None = None
        self._want_running = False   # intent — distinguishes a deliberate stop from a crash
        self._lock = threading.RLock()
        self._last_exit_code: int | None = None   # last observed dead-at-boot returncode (diagnostics)

    def start(self) -> None:
        with self._lock:
            self._want_running = True
            if self._proc and self._proc.poll() is None:
                return
            self._proc = subprocess.Popen([self.xray_bin, "-config", self.config_path])

    def stop(self) -> None:
        with self._lock:
            self._want_running = False
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait()
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
                return False
            try:
                if self._ready_check():
                    return True
            except Exception:
                pass
            time.sleep(self.READY_STEP)
        # budget exhausted: ready if still alive, else it died
        return proc.poll() is None

    def status(self) -> SupervisorStatus:
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
            return {"running": running, "pid": self._proc.pid if running else None}

    def state(self) -> str:
        """3-way state for the sidebar xray-core box: 'working' (running) |
        'error' (we wanted it running but it died) | 'stopped' (deliberate)."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return "working"
            return "error" if self._want_running else "stopped"
