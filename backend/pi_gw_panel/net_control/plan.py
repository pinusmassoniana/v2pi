from dataclasses import dataclass
from pi_gw_panel.config import Settings


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

    @classmethod
    def from_settings(cls, s: Settings) -> "NetPlan":
        return cls(
            tproxy_port=s.tproxy_port, fwmark=s.fwmark, egress_mark=s.egress_mark,
            table=s.table, segment_iface=s.segment_iface, segment_ip=s.segment_ip,
            dhcp_start=s.dhcp_start, dhcp_end=s.dhcp_end, dhcp_lease=s.dhcp_lease,
            client_dns=s.client_dns, client_dns6=s.client_dns6, dnsmasq_leases=s.dnsmasq_leases,
            segment_ip6=s.segment_ip6, tproxy_port6=s.tproxy_port6,
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
        )


@dataclass
class NetResult:
    ok: bool
    rendered: str = ""
    error: str = ""
