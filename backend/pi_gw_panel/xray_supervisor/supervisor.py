import subprocess
from typing import TypedDict


class SupervisorStatus(TypedDict):
    running: bool
    pid: int | None


class XraySupervisor:
    """Owns the xray child process. reload = restart (xray has no live-reload signal)."""

    def __init__(self, xray_bin: str, config_path: str):
        self.xray_bin = xray_bin
        self.config_path = config_path
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

    def status(self) -> SupervisorStatus:
        running = self._proc is not None and self._proc.poll() is None
        return {"running": running, "pid": self._proc.pid if running else None}

    def state(self) -> str:
        """3-way state for the sidebar xray-core box: 'working' (running) |
        'error' (we wanted it running but it died) | 'stopped' (deliberate)."""
        if self._proc is not None and self._proc.poll() is None:
            return "working"
        return "error" if self._want_running else "stopped"
