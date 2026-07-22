"""DHCPv6-PD client (Phase D): run `dhclient -6 -P` (isc-dhcp-client) on the mgmt/uplink leg
requesting a delegated prefix (IA_PD only — the host keeps its own SLAAC address on a separate IA,
no conflict). When a prefix lands, the caller derives the segment /64 and re-addresses it; with no
delegation the gateway falls back to an auto-ULA so v6 still works (tunnelled). The logic here is
unit-tested; the dhclient child is Pi-only."""
import ipaddress
import logging
import os
import shlex
import subprocess
import threading


_log = logging.getLogger("pi_gw_panel")
_UNSEEN = object()


def derive_segment_prefix(delegated: str, vlan_id: int) -> str | None:
    """Carve a /64 for the segment out of the delegated prefix, indexing the /64 subnets by the
    VLAN id. None if the delegation is too small to host that subnet."""
    if not isinstance(vlan_id, int) or isinstance(vlan_id, bool) or not 0 <= vlan_id <= 65535:
        return None
    try:
        net = ipaddress.ip_network(delegated, strict=False)
    except ValueError:
        return None
    if net.version != 6 or net.prefixlen > 64:
        return None
    if net.prefixlen == 64:
        return net.with_prefixlen if vlan_id == 0 else None
    for i, sub in enumerate(net.subnets(new_prefix=64)):
        if i == vlan_id:
            return sub.with_prefixlen
        if i > vlan_id:
            break
    return None


class PdClient:
    """Supervises a `dhclient -6 -P` child requesting IA_PD on the uplink. `script` is the
    state-change hook dhclient runs on (re)delegation; `popen` is the injectable spawn seam."""

    def __init__(self, mgmt_iface: str, script: str, popen=subprocess.Popen,
                 on_prefix_change=None, poll_interval: float = 1.0):
        self.mgmt_iface = mgmt_iface
        self.script = script
        self.prefix_file = f"{script}.prefix"
        self._popen = popen
        self._proc = None
        self._on_prefix_change = on_prefix_change
        self._poll_interval = poll_interval
        self._last_prefix = _UNSEEN
        self._stop_event = threading.Event()
        self._thread = None

    def set_callback(self, callback) -> None:
        self._on_prefix_change = callback

    def write_hook(self) -> None:
        """Install dhclient's hook. The child reports leases through an atomic state file."""
        parent = os.path.dirname(self.script)
        if parent:
            os.makedirs(parent, exist_ok=True)
        state = shlex.quote(self.prefix_file)
        body = f"""#!/bin/sh
set -eu
state={state}
case "${{reason:-}}" in
  BOUND6|RENEW6|REBIND6|REBOOT6)
    prefix="${{new_ip6_prefix:-${{new_ip6_prefixes:-}}}}"
    plen="${{new_ip6_prefixlen:-}}"
    [ -n "${{prefix}}" ] || exit 0
    set -- ${{prefix}}
    prefix="$1"
    case "${{prefix}}" in */*) value="${{prefix}}" ;; *) value="${{prefix}}/${{plen:-64}}" ;; esac
    tmp="${{state}}.$$"
    printf '%s\n' "${{value}}" > "${{tmp}}"
    mv "${{tmp}}" "${{state}}"
    ;;
  EXPIRE6|RELEASE6|STOP6)
    rm -f "${{state}}"
    ;;
esac
exit 0
"""
        tmp = f"{self.script}.tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            f.write(body)
        os.chmod(tmp, 0o755)
        os.replace(tmp, self.script)

    def poll_once(self) -> None:
        """Read one hook update and notify only when the observed value changed."""
        try:
            with open(self.prefix_file) as f:
                prefix = f.read().strip() or None
        except OSError:
            prefix = None
        if prefix == self._last_prefix:
            return
        self._last_prefix = prefix
        if self._on_prefix_change is not None:
            self._on_prefix_change(prefix)

    def _watch(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception:
                _log.warning("DHCPv6-PD prefix callback failed", exc_info=True)
            self._stop_event.wait(self._poll_interval)

    def _ensure_watcher(self) -> None:
        if self._on_prefix_change is None or self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._watch, name="dhcpv6-pd-prefix", daemon=True)
        self._thread.start()

    def clear_state(self) -> None:
        """Discard the hook's last delegation when leaving auto mode."""
        try:
            os.unlink(self.prefix_file)
        except FileNotFoundError:
            pass
        self._last_prefix = _UNSEEN

    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._ensure_watcher()
            return
        self.write_hook()
        # -6 -P: DHCPv6 prefix-delegation mode (IA_PD); -d: stay in the foreground as our child;
        # -sf: the state-change script run on (re)delegation. PD-only — we don't take an address.
        self._proc = self._popen(["dhclient", "-6", "-P", "-d", "-sf", self.script, self.mgmt_iface])
        self._ensure_watcher()

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        self._proc = None
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self._poll_interval * 2))
            self._thread = None
        self._last_prefix = _UNSEEN
