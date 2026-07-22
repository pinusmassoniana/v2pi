"""Owns the panel's dnsmasq child (segment DHCP + IPv6 RA). Mirrors XraySupervisor: `apply`
writes the rendered config and (re)starts only when the text changed (dnsmasq has no reliable
live-reload for dhcp-range/RA, so reload = restart). `popen` is the injectable spawn seam."""
import os
import subprocess
import tempfile
import threading
import time
from typing import TypedDict


class SupervisorStatus(TypedDict):
    running: bool
    pid: int | None


class DnsmasqSupervisor:
    def __init__(self, dnsmasq_bin: str, conf_path: str, popen=subprocess.Popen,
                 run=subprocess.run, sleep=time.sleep):
        self.dnsmasq_bin = dnsmasq_bin
        self.conf_path = conf_path
        self._popen = popen
        self._run = run
        self._sleep = sleep
        self._proc = None
        self._last_text: str | None = None
        self._lock = threading.RLock()

    def apply(self, text: str) -> None:
        with self._lock:
            changed = text != self._last_text
            if not changed and self._running():
                return
            parent = os.path.dirname(self.conf_path) or "."
            os.makedirs(parent, exist_ok=True)
            fd, candidate = tempfile.mkstemp(prefix=".dnsmasq-", suffix=".conf", dir=parent)
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(text)
                    f.flush()
                    os.fsync(f.fileno())
                try:
                    self._run(
                        [self.dnsmasq_bin, "--test", f"--conf-file={candidate}"],
                        capture_output=True, text=True, timeout=5, check=True)
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
                    detail = (getattr(exc, "stderr", None) or str(exc)).strip()
                    raise RuntimeError(f"dnsmasq config validation failed: {detail}") from exc

                previous_text = self._last_text
                previous_running = self._running()
                self.stop()
                os.replace(candidate, self.conf_path)
                candidate = ""
                try:
                    self._spawn()
                    self._sleep(0.1)
                    if not self._running():
                        raise RuntimeError("dnsmasq exited during readiness check")
                except Exception as exc:
                    self.stop()
                    self._restore_config(previous_text)
                    self._last_text = previous_text
                    if previous_running and previous_text is not None:
                        try:
                            self._spawn()
                        except Exception:
                            self._proc = None
                    raise RuntimeError(f"dnsmasq candidate failed: {exc}") from exc
                self._last_text = text
            finally:
                if candidate and os.path.exists(candidate):
                    os.unlink(candidate)

    def _spawn(self) -> None:
        # --no-daemon: stay in the foreground as our child; --conf-file pins exactly our rendered
        # config (no /etc/dnsmasq.d merge).
        self._proc = self._popen([self.dnsmasq_bin, "--no-daemon", f"--conf-file={self.conf_path}"])

    def _restore_config(self, text: str | None) -> None:
        if text is None:
            try:
                os.unlink(self.conf_path)
            except FileNotFoundError:
                pass
            return
        parent = os.path.dirname(self.conf_path) or "."
        fd, tmp = tempfile.mkstemp(prefix=".dnsmasq-rollback-", dir=parent)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.conf_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def stop(self) -> None:
        with self._lock:
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
