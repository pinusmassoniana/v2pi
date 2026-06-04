import os
import time
from pi_gw_panel.config import Settings
from pi_gw_panel.health.snapshot import active_health


def segment_up(iface: str, sysfs: str = "/sys/class/net") -> bool | None:
    """Read ``<sysfs>/<iface>/operstate``. True/False on up/down; None when the
    path is absent (dev/macOS — no Linux sysfs)."""
    try:
        with open(os.path.join(sysfs, iface, "operstate")) as f:
            return f.read().strip() == "up"
    except OSError:
        return None


def dhcp_leases(leases_path: str, now: float | None = None) -> list[dict]:
    """Parse the dnsmasq leases file → unexpired leases ``[{ip, mac, hostname, expiry}]``
    (P5). Each line is ``<expiry_epoch> <mac> <ip> <hostname> <client-id>``; expiry 0 means
    no expiry. Expired leases are dropped (audit F4). Empty/absent file → []."""
    now = time.time() if now is None else now
    out: list[dict] = []
    try:
        with open(leases_path) as f:
            for line in f:
                parts = line.split()
                if len(parts) < 4:
                    continue
                try:
                    expiry = int(parts[0])
                except ValueError:
                    continue
                if expiry != 0 and expiry < now:
                    continue                                   # lease has expired
                host = parts[3] if parts[3] != "*" else ""
                out.append({"ip": parts[2], "mac": parts[1], "hostname": host, "expiry": expiry})
    except OSError:
        return []
    return out


def dhcp_clients(leases_path: str, now: float | None = None) -> int:
    """Count of currently-leased (unexpired) clients; 0 when the file is absent."""
    return len(dhcp_leases(leases_path, now))


def _tunnel(store) -> dict:
    """Active node's tunnel egress + freshness via the shared health snapshot (F3)."""
    a = active_health(store)
    if a is None:
        return {"real_ok": None, "latency_ms": None, "egress_ip": None, "checked_at": None}
    return {"real_ok": a["real_ok"], "latency_ms": a["latency_ms"],
            "egress_ip": a["egress_ip"], "checked_at": a["checked_at"]}


def network_status(store, settings: Settings, *, sysfs: str = "/sys/class/net",
                   leases_path: str | None = None) -> dict:
    """Live gateway status: segment link, DHCP clients (+ list), tunnel egress. Real
    checks are Pi-only; dev returns unknown/0 gracefully (paths injected in tests)."""
    iface = store.get_setting("segment_iface") or settings.segment_iface
    clients = dhcp_leases(leases_path or settings.dnsmasq_leases)
    return {
        "segment_up": segment_up(iface, sysfs=sysfs),
        "dhcp_clients": len(clients),
        "clients": clients,
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
