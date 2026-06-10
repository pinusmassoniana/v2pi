import subprocess
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
    """

    READY_TIMEOUT = 2.0
    READY_STEP = 0.05

    def __init__(self, xray_bin: str, config_path: str, ready_check=None):
        self.xray_bin = xray_bin
        self.config_path = config_path
        self._ready_check = ready_check
        self._proc: subprocess.Popen | None = None
        self._want_running = False   # intent — distinguishes a deliberate stop from a crash

    def start(self) -> None:
        self._want_running = True
        if self._proc and self._proc.poll() is None:
            return
        self._proc = subprocess.Popen([self.xray_bin, "-config", self.config_path])

    def stop(self) -> None:
        self._want_running = False
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        self._proc = None

    def reload(self) -> None:
        self.stop()
        self.start()
        self._wait_ready()

    def _wait_ready(self) -> None:
        """Poll `ready_check` until it passes, the process dies, or the budget runs out.
        Best-effort: a timeout proceeds anyway (this only shrinks the window where freshly
        tproxy'd client packets hit a not-yet-listening xray and get RST)."""
        if self._ready_check is None:
            return
        deadline = time.monotonic() + self.READY_TIMEOUT
        while time.monotonic() < deadline:
            if self._proc is None or self._proc.poll() is not None:
                return                      # died at boot — nothing to wait for
            try:
                if self._ready_check():
                    return
            except Exception:
                pass
            time.sleep(self.READY_STEP)

    def status(self) -> SupervisorStatus:
        running = self._proc is not None and self._proc.poll() is None
        return {"running": running, "pid": self._proc.pid if running else None}

    def state(self) -> str:
        """3-way state for the sidebar xray-core box: 'working' (running) |
        'error' (we wanted it running but it died) | 'stopped' (deliberate)."""
        if self._proc is not None and self._proc.poll() is None:
            return "working"
        return "error" if self._want_running else "stopped"
