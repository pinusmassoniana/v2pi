"""DHCPv6-PD client (Phase D): run `dhclient -6 -P` (isc-dhcp-client) on the mgmt/uplink leg
requesting a delegated prefix (IA_PD only — the host keeps its own SLAAC address on a separate IA,
no conflict). When a prefix lands, the caller derives the segment /64 and re-addresses it; with no
delegation the gateway falls back to an auto-ULA so v6 still works (tunnelled). The logic here is
unit-tested; the dhclient child is Pi-only."""
import ipaddress
import subprocess


def derive_segment_prefix(delegated: str, vlan_id: int) -> str | None:
    """Carve a /64 for the segment out of the delegated prefix, indexing the /64 subnets by the
    VLAN id. None if the delegation is too small to host that subnet."""
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

    def __init__(self, mgmt_iface: str, script: str, popen=subprocess.Popen):
        self.mgmt_iface = mgmt_iface
        self.script = script
        self._popen = popen
        self._proc = None

    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        # -6 -P: DHCPv6 prefix-delegation mode (IA_PD); -d: stay in the foreground as our child;
        # -sf: the state-change script run on (re)delegation. PD-only — we don't take an address.
        self._proc = self._popen(["dhclient", "-6", "-P", "-d", "-sf", self.script, self.mgmt_iface])

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        self._proc = None
