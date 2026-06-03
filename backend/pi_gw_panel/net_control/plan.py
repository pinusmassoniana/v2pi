from dataclasses import dataclass
from pi_gw_panel.config import Settings


# Pi-side net config that is editable end-to-end (segment iface/IP, DHCP, client DNS)
# defaults to the config values but can be overridden per-field in the settings k/v.
_EDITABLE = ("segment_iface", "segment_ip", "dhcp_start", "dhcp_end", "dhcp_lease", "client_dns")


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
    kill_switch: bool = False

    @classmethod
    def from_settings(cls, s: Settings) -> "NetPlan":
        return cls(
            tproxy_port=s.tproxy_port, fwmark=s.fwmark, egress_mark=s.egress_mark,
            table=s.table, segment_iface=s.segment_iface, segment_ip=s.segment_ip,
            dhcp_start=s.dhcp_start, dhcp_end=s.dhcp_end, dhcp_lease=s.dhcp_lease,
            client_dns=s.client_dns,
        )

    @classmethod
    def from_store(cls, store, s: Settings) -> "NetPlan":
        """Resolve each editable field as store-override-or-config; system knobs
        (tproxy port / marks / table) stay config-only; kill-switch from the k/v flag."""
        ov = {k: (store.get_setting(k) or getattr(s, k)) for k in _EDITABLE}
        return cls(
            tproxy_port=s.tproxy_port, fwmark=s.fwmark, egress_mark=s.egress_mark,
            table=s.table, **ov,
            kill_switch=(store.get_setting("kill_switch_enabled") or "0") == "1",
        )


@dataclass
class NetResult:
    ok: bool
    rendered: str = ""
    error: str = ""
