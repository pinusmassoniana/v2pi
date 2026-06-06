import ipaddress
from dataclasses import dataclass
from pi_gw_panel.config import Settings


def net24(ip: str) -> str:
    """The /24 network containing `ip`, as a CIDR string ('' when blank/invalid). The panel's
    segment and home LAN are /24 by convention (matches the DHCP range, recommendations, etc.)."""
    ip = (ip or "").strip()
    try:
        return str(ipaddress.ip_network(f"{ip}/24", strict=False)) if ip else ""
    except ValueError:
        return ""


# Pi-side net config that is editable end-to-end (segment iface/IP/v6-prefix, DHCP, client DNS)
# defaults to the config values but can be overridden per-field in the settings k/v.
_EDITABLE = ("segment_iface", "segment_ip", "segment_ip6",
             "dhcp_start", "dhcp_end", "dhcp_lease", "client_dns", "client_dns6")


@dataclass
class NetPlan:
    tproxy_port: int
    fwmark: int
    egress_mark: int
    table: int
    segment_iface: str
    segment_ip: str
    dhcp_start: str
    dhcp_end: str
    dhcp_lease: str
    client_dns: str
    client_dns6: str = "2606:4700:4700::1111"
    dnsmasq_leases: str = "data/dnsmasq.leases"
    kill_switch: bool = False
    # IPv6 tunnel (opt-in): tunnel segment client v6 through xray to tproxy_port6.
    ipv6_enabled: bool = False
    segment_ip6: str = ""
    tproxy_port6: int = 52346
    # LAN access: forward + masquerade segment -> home LAN (the mgmt leg). mgmt_iface/_ip give
    # the egress iface + home-LAN /24 for those rules. Dataclass default off so directly-built
    # plans (tests) stay isolated; from_settings/from_store carry the real config/DB value.
    lan_access: bool = False
    mgmt_iface: str = "eth0"
    mgmt_ip: str = ""

    @classmethod
    def from_settings(cls, s: Settings) -> "NetPlan":
        return cls(
            tproxy_port=s.tproxy_port, fwmark=s.fwmark, egress_mark=s.egress_mark,
            table=s.table, segment_iface=s.segment_iface, segment_ip=s.segment_ip,
            dhcp_start=s.dhcp_start, dhcp_end=s.dhcp_end, dhcp_lease=s.dhcp_lease,
            client_dns=s.client_dns, client_dns6=s.client_dns6, dnsmasq_leases=s.dnsmasq_leases,
            segment_ip6=s.segment_ip6, tproxy_port6=s.tproxy_port6,
            lan_access=s.lan_access, mgmt_iface=s.mgmt_iface, mgmt_ip=s.mgmt_ip,
        )

    @classmethod
    def from_store(cls, store, s: Settings) -> "NetPlan":
        """Resolve each editable field as store-override-or-config; system knobs
        (tproxy ports / marks / table) stay config-only; kill-switch + ipv6 from k/v flags."""
        ov = {k: (store.get_setting(k) or getattr(s, k)) for k in _EDITABLE}
        return cls(
            tproxy_port=s.tproxy_port, fwmark=s.fwmark, egress_mark=s.egress_mark,
            table=s.table, tproxy_port6=s.tproxy_port6, dnsmasq_leases=s.dnsmasq_leases, **ov,
            kill_switch=(store.get_setting("kill_switch_enabled") or "0") == "1",
            ipv6_enabled=(store.get_setting("ipv6_enabled") or "0") == "1",
            lan_access=(store.get_setting("lan_access_enabled") or ("1" if s.lan_access else "0")) == "1",
            mgmt_iface=s.mgmt_iface, mgmt_ip=s.mgmt_ip,
        )


@dataclass
class NetResult:
    ok: bool
    rendered: str = ""
    error: str = ""
