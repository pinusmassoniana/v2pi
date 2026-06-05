"""Owns the panel's dnsmasq child (segment DHCP + IPv6 RA). Mirrors XraySupervisor: `apply`
writes the rendered config and (re)starts only when the text changed (dnsmasq has no reliable
live-reload for dhcp-range/RA, so reload = restart). `popen` is the injectable spawn seam."""
import subprocess
from typing import TypedDict


class SupervisorStatus(TypedDict):
    running: bool
    pid: int | None


class DnsmasqSupervisor:
    def __init__(self, dnsmasq_bin: str, conf_path: str, popen=subprocess.Popen):
        self.dnsmasq_bin = dnsmasq_bin
        self.conf_path = conf_path
        self._popen = popen
        self._proc = None
        self._last_text: str | None = None

    def apply(self, text: str) -> None:
        changed = text != self._last_text
        with open(self.conf_path, "w") as f:
            f.write(text)
        self._last_text = text
        if changed or not self._running():
            self._restart()

    def _restart(self) -> None:
        self.stop()
        # --no-daemon: stay in the foreground as our child; --conf-file pins exactly our rendered
        # config (no /etc/dnsmasq.d merge).
        self._proc = self._popen([self.dnsmasq_bin, "--no-daemon", f"--conf-file={self.conf_path}"])

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        self._proc = None

    def _running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def status(self) -> SupervisorStatus:
        running = self._running()
        return {"running": running, "pid": self._proc.pid if running else None}
