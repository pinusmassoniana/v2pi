import os
from pi_gw_panel.config import Settings


def segment_up(iface: str, sysfs: str = "/sys/class/net") -> bool | None:
    """Read ``<sysfs>/<iface>/operstate``. True/False on up/down; None when the
    path is absent (dev/macOS — no Linux sysfs)."""
    try:
        with open(os.path.join(sysfs, iface, "operstate")) as f:
            return f.read().strip() == "up"
    except OSError:
        return None


def dhcp_clients(leases_path: str) -> int:
    """Count active leases (non-blank lines in the dnsmasq leases file); 0 when absent."""
    try:
        with open(leases_path) as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def _tunnel(store) -> dict:
    """Active node's tunnel egress, reusing the Wave-3a node_health snapshot."""
    aid = store.get_setting("active_node_id")
    h = store.get_health(int(aid)) if aid else None
    if h is None:
        return {"real_ok": None, "latency_ms": None, "egress_ip": None}
    return {"real_ok": h.last_real_ok, "latency_ms": h.last_real_ms, "egress_ip": h.egress_ip}


def network_status(store, settings: Settings, *, sysfs: str = "/sys/class/net",
                   leases_path: str | None = None) -> dict:
    """Live gateway status: segment link, DHCP client count, tunnel egress. Real
    checks are Pi-only; dev returns unknown/0 gracefully (paths injected in tests)."""
    iface = store.get_setting("segment_iface") or settings.segment_iface
    return {
        "segment_up": segment_up(iface, sysfs=sysfs),
        "dhcp_clients": dhcp_clients(leases_path or settings.dnsmasq_leases),
        "tunnel": _tunnel(store),
    }


def router_recommendations(settings: Settings) -> list[dict]:
    """Static, config-derived guidance for the one box the panel never touches —
    the router. The live-status panel verifies the result visually."""
    iface = settings.segment_iface
    vlan = iface.split(".")[-1] if "." in iface else "?"
    return [
        {"title": f"Create VLAN {vlan}",
         "detail": f"Add VLAN {vlan} on the router and tag the client switch port to it "
                   f"(the Pi's client leg is {iface})."},
        {"title": f"Disable the router's DHCP on VLAN {vlan}",
         "detail": f"The Pi serves DHCP on this segment ({settings.dhcp_start}–{settings.dhcp_end}); "
                   f"two DHCP servers on one VLAN conflict."},
        {"title": "Give the Pi's Home leg internet",
         "detail": f"The Pi reaches the tunnel through its Home leg {settings.mgmt_iface} "
                   f"({settings.mgmt_ip}); keep that port on your normal LAN with internet access."},
    ]
