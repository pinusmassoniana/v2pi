import ipaddress
import os
import socket
import subprocess
import time
from pi_gw_panel.config import Settings
from pi_gw_panel.health.snapshot import active_health


def _run_text(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def foreign_ra(iface: str, run=_run_text) -> bool | None:
    """Detect another router advertising on the client segment (the leak we hit with the
    Keenetic): any IPv6 neighbor on `iface` flagged `router`. The Pi is the gateway and does not
    appear as a router-neighbor of itself, so a `router` entry here is foreign. None when we
    can't tell (dev / `ip` absent). `run` is an injectable seam for tests."""
    try:
        text = run(["ip", "-6", "neigh", "show", "dev", iface])
    except (subprocess.CalledProcessError, OSError):
        return None
    return any("router" in line.split()[2:] for line in text.splitlines() if line.strip())


def segment_prefix6(iface: str, proc_path: str = "/proc/net/if_inet6", read=None) -> str | None:
    """The segment iface's GLOBAL IPv6 address/prefix as the host sees it — used by the
    DHCPv6-PD `auto` mode to *observe* the prefix delegated to the segment (the PD client +
    RA run on the host, the panel just reads the result). Parses `/proc/net/if_inet6`
    (`<32hex-addr> <ifidx> <plen-hex> <scope-hex> <flags> <name>`; scope 00 = global). Returns
    e.g. `2001:db8:0:2::1/64`, or None when there's no global v6 (dev / not delegated yet).
    `read` is an injectable seam for tests."""
    try:
        text = read() if read is not None else open(proc_path).read()
    except OSError:
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 6 or parts[5] != iface or parts[3] != "00":   # match iface + global scope
            continue
        try:
            if int(parts[4], 16) & 0x01:      # F: skip IFA_F_TEMPORARY (privacy) addresses
                continue
            addr = ipaddress.IPv6Address(int(parts[0], 16))
            return f"{addr.compressed}/{int(parts[2], 16)}"
        except ValueError:
            continue
    return None


def uplink_up(host: str = "1.1.1.1", port: int = 443, timeout: float = 1.5,
              connect=socket.create_connection) -> bool:
    """Direct TCP reachability of the WAN/uplink (the Pi's Home leg), bypassing the tunnel
    (C1). Lets the UI tell a bad node (tunnel red, uplink green) from a dead internet
    (both red). Only invoked on the real Pi backend — dev/CI report 'unknown'."""
    try:
        connect((host, port), timeout).close()
        return True
    except OSError:
        return False


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
                   leases_path: str | None = None, uplink_check=lambda: None) -> dict:
    """Live gateway status: segment link, uplink, DHCP clients (+ list), tunnel egress.
    Real checks are Pi-only; dev returns unknown/0 gracefully (paths/probes injected in
    tests). `uplink_check` defaults to 'unknown' so a bare call never touches the network —
    the route passes the real probe only on the Linux backend (C1)."""
    iface = store.get_setting("segment_iface") or settings.segment_iface
    clients = dhcp_leases(leases_path or settings.dnsmasq_leases)
    return {
        "segment_up": segment_up(iface, sysfs=sysfs),
        "uplink": uplink_check(),
        "dhcp_clients": len(clients),
        "clients": clients,
        "tunnel": _tunnel(store),
    }


def router_recommendations(settings: Settings, ipv6_enabled: bool = False,
                           segment_ip6: str = "") -> list[dict]:
    """Static, config-derived guidance for the one box the panel never touches —
    the router. The live-status panel verifies the result visually. When the IPv6 tunnel is
    on, append the v6 prefix/RA guidance (the panel tunnels v6 but RA is host/router-managed)."""
    iface = settings.segment_iface
    vlan = iface.split(".")[-1] if "." in iface else "?"
    recs = [
        {"title": f"Create VLAN {vlan}",
         "detail": f"Add VLAN {vlan} on the router and tag the client switch port to it "
                   f"(the Pi's client leg is {iface})."},
        {"title": f"Disable the router's DHCP on VLAN {vlan}",
         "detail": f"The Pi serves DHCP + DNS on this segment ({settings.dhcp_start}–{settings.dhcp_end}); "
                   f"two DHCP servers on one VLAN conflict."},
        {"title": "Give the Pi's Home leg internet",
         "detail": f"The Pi reaches the tunnel through its Home leg {settings.mgmt_iface} "
                   f"({settings.mgmt_ip}); keep that port on your normal LAN with internet access."},
    ]
    if ipv6_enabled:
        if segment_ip6.strip().lower() == "auto":
            first = {"title": "Enable DHCPv6-PD on the router",
                     "detail": f"`auto` mode: the panel runs a DHCPv6-PD client (dhclient -6 -P) on "
                               f"{settings.mgmt_iface} and requests a prefix — enable prefix "
                               f"delegation on the router; the panel shows it once it lands."}
        else:
            prefix = segment_ip6 or "your /64"
            first = {"title": f"Delegate an IPv6 /64 to VLAN {vlan}",
                     "detail": f"Route a v6 /64 to this segment — DHCPv6-PD on the router, or a "
                               f"static route of {prefix} to the Pi's Home leg {settings.mgmt_iface}. "
                               f"(Set the prefix to `auto` to read it from a host PD client instead.)"}
        recs += [
            first,
            {"title": f"Disable the router's IPv6 / Router Advertisement on VLAN {vlan}",
             "detail": "The Pi advertises IPv6 (RA) on this segment itself; a second router "
                       "advertising its ISP prefix here makes clients leak around the tunnel "
                       "(they'd pick the router's prefix, not the Pi's)."},
            {"title": "Use a node with IPv6 egress",
             "detail": "v6 traffic exits via the active node; pick one with working IPv6 or v6-only "
                       "sites will fail (no leak — they just won't connect)."},
        ]
    return recs
