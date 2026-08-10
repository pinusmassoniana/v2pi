"""Owns the panel's dnsmasq child (segment DHCP + IPv6 RA). Mirrors XraySupervisor: `apply`
writes the rendered config and (re)starts only when the text changed (dnsmasq has no reliable
live-reload for dhcp-range/RA, so reload = restart). `popen` is the injectable spawn seam."""
import os
import subprocess
import tempfile
import threading
import time
from typing import TypedDict

from pi_gw_panel.proc import stop_process


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
                # Deliberately before `os.replace`: if the running child cannot be stopped this
                # raises, and an apply that could not free the DHCP/DNS sockets must not go on
                # to install the new config and spawn a second dnsmasq onto them.
                self.stop()
                os.replace(candidate, self.conf_path)
                candidate = ""
                try:
                    self._spawn()
                    self._sleep(0.1)
                    if not self._running():
                        raise RuntimeError("dnsmasq exited during readiness check")
                except Exception as exc:
                    detail = str(exc)
                    try:
                        self.stop()
                        stopped = True
                    except RuntimeError as stop_exc:
                        # The rollback still has to happen — the config on disk must end up
                        # being the one we mean to run — but a candidate we could not kill
                        # rules out putting the previous one next to it, and leaves us unable
                        # to say what text the surviving child is serving.
                        stopped = False
                        detail = f"{detail}; {stop_exc}"
                    self._restore_config(previous_text)
                    self._last_text = previous_text if stopped else None
                    if stopped and previous_running and previous_text is not None:
                        try:
                            self._spawn()
                        except Exception:
                            self._proc = None
                    raise RuntimeError(f"dnsmasq candidate failed: {detail}") from exc
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
        """Stop the child, bounded. Raises when it outlived SIGKILL.

        Every call happens under the apply-lock (provisioning, a settings apply), so the wait
        that used to follow `kill()` unbounded could hold that lock — and the process-wide DB
        lock behind it — for as long as the child stayed unkillable. And a survivor must not be
        forgotten: dropping the handle would make `status()` report "not running" while a
        dnsmasq is still answering DHCP on the segment, and let the next `apply()` start a
        second one beside it. So the handle is kept and the failure is raised.
        """
        with self._lock:
            if not stop_process(self._proc, name="dnsmasq"):
                raise RuntimeError(
                    f"dnsmasq (pid {getattr(self._proc, 'pid', '?')}) did not stop")
            self._proc = None

    def _running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def status(self) -> SupervisorStatus:
        running = self._running()
        return {"running": running, "pid": self._proc.pid if running else None}
