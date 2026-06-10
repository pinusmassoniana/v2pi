import os
from collections.abc import Mapping
from dataclasses import dataclass


def _packaged_static() -> str:
    """The SPA bundled next to this package (built into pi_gw_panel/static); '' if absent."""
    p = os.path.join(os.path.dirname(__file__), "static")
    return p if os.path.isdir(p) else ""


@dataclass
class Settings:
    xray_bin: str = "xray"
    data_dir: str = "data"
    db_path: str = "data/pi_gw_panel.sqlite"
    config_path: str = "data/xray.json"
    lastgood_path: str = "data/xray.lastgood.json"
    # tproxy / marks — match the live Pi: xray dokodemo on :52345, client traffic
    # marked 0x40 -> tproxy; xray's own egress marked 0x80 (SO_MARK) so nft skips it
    # (anti-loop); policy-routing table 100.
    tproxy_port: int = 52345
    tproxy_port6: int = 52346   # IPv6 dokodemo tproxy inbound (separate from v4 to avoid v6only edge-cases)
    fwmark: int = 0x40
    egress_mark: int = 0x80
    table: int = 100
    # segment = client-facing leg (VLAN2): dnsmasq DHCP + tproxy live here
    segment_iface: str = "eth0.2"
    segment_ip: str = "192.168.10.2"
    segment_ip6: str = ""              # segment's static IPv6 /64 (opt-in IPv6 tunnel; RA host-managed)
    dhcp_start: str = "192.168.10.30"
    dhcp_end: str = "192.168.10.200"
    dhcp_lease: str = "12h"
    client_dns: str = "1.1.1.1"   # handed to clients via DHCP; tproxy'd through the tunnel
    client_dns6: str = "2606:4700:4700::1111"   # v6 DNS handed to clients via RA (when IPv6 on)
    dnsmasq_bin: str = "dnsmasq"
    geoip_db: str = "/usr/local/share/dbip-country-lite.mmdb"  # egress IP→country flag (absent in dev → no flag)
    dnsmasq_leases: str = "data/dnsmasq.leases"  # the panel's own dnsmasq leasefile (under data_dir)
    # mgmt = Home leg: panel bind + SSH + tunnel egress
    mgmt_iface: str = "eth0"
    mgmt_ip: str = "192.168.1.120"
    # LAN access (default on): segment clients reach the home LAN (the mgmt /24) directly —
    # forward-accept in DOCKER-USER + masquerade. Off = the segment is isolated from the home
    # LAN. Internet stays tunnel-only either way (tproxy untouched; forward/NAT scoped to the LAN).
    lan_access: bool = True
    doh_url: str = "https://1.1.1.1/dns-query"   # xray's own DoH resolver
    # HTTP layer (Plan 2)
    password: str = "changeme"  # DEPRECATED (Wave 3a): unused — the panel credential
                                # now lives in the DB (auth_username/auth_password_hash),
                                # created at first run via /api/setup. Kept to avoid churn.
    session_secret: str = "dev-insecure-secret"
    login_lockout_sec: int = 60   # per-IP lockout after 5 failed logins (e2e overrides it down)
    bind_host: str = "127.0.0.1"  # prod binds mgmt_ip (Home); dev = localhost
    static_dir: str = ""
    local_proxy_port: int = 10808  # gated 127.0.0.1 http inbound for tunneled sub-fetch
    base_dir: str = ""

    def __post_init__(self) -> None:
        if self.base_dir:
            for attr in ("data_dir", "db_path", "config_path", "lastgood_path"):
                val = getattr(self, attr)
                if not os.path.isabs(val):
                    setattr(self, attr, os.path.join(self.base_dir, val))

    def ensure_dirs(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        for p in (self.db_path, self.config_path, self.lastgood_path):
            parent = os.path.dirname(p)
            if parent:
                os.makedirs(parent, exist_ok=True)

    @classmethod
    def from_env(cls, env: "Mapping[str, str] | None" = None) -> "Settings":
        """Build Settings from PI_GW_* env vars (container/prod entrypoint). Data paths
        nest under PI_GW_DATA_DIR; static_dir defaults to the packaged SPA; the session
        secret defaults empty (the entrypoint refuses to start without a real one)."""
        env = os.environ if env is None else env
        data = env.get("PI_GW_DATA_DIR", "data")
        return cls(
            data_dir=data,
            db_path=os.path.join(data, "pi_gw_panel.sqlite"),
            config_path=os.path.join(data, "xray.json"),
            lastgood_path=os.path.join(data, "xray.lastgood.json"),
            bind_host=env.get("PI_GW_BIND_HOST", "0.0.0.0"),   # reachable by default; auth-gated
            static_dir=env.get("PI_GW_STATIC_DIR", _packaged_static()),
            xray_bin=env.get("PI_GW_XRAY_BIN", "xray"),
            session_secret=env.get("PI_GW_SESSION_SECRET", ""),
            login_lockout_sec=int(env.get("PI_GW_LOGIN_LOCKOUT_SEC", "60")),
            dnsmasq_leases=env.get("PI_GW_DNSMASQ_LEASES", os.path.join(data, "dnsmasq.leases")),
            client_dns6=env.get("PI_GW_CLIENT_DNS6", "2606:4700:4700::1111"),
            geoip_db=env.get("PI_GW_GEOIP_DB", "/usr/local/share/dbip-country-lite.mmdb"),
        )

    # Log file paths derive from data_dir (Wave 3a logs viewer) so they always track
    # the active data dir (incl. base_dir / test tmp dirs).
    @property
    def xray_error_log(self) -> str:
        return os.path.join(self.data_dir, "xray-error.log")

    @property
    def xray_access_log(self) -> str:
        return os.path.join(self.data_dir, "xray-access.log")

    @property
    def app_log(self) -> str:
        return os.path.join(self.data_dir, "app.log")


# Global non-tuning settings live in the SQLite `settings` k/v table (string values).
# Wave 2 moved the anti-DPI tuning knobs (fragmentation/mux/DoH/fingerprint) into
# per-node tuning *profiles*; what remains here is `tunneled_fetch` plus the
# routing default action and the health/auto-failover knobs.
SETTINGS_DEFAULTS = {
    "tunneled_fetch": "1",
    "routing_default_action": "proxy",
    "health_enabled": "1",
    "health_interval": "1800",
    "health_hysteresis": "3",
    "health_probe_url": "https://api.ipify.org?format=json",
    # v6-only echo (AAAA-only host → forces v6 egress) for the per-node IPv6 egress readout
    "health_probe_url6": "https://api6.ipify.org?format=json",
    "failover_enabled": "1",
    "failover_cooldown": "120",
    # Wave 3a — xray StatsService → live traffic graph
    "stats_enabled": "1",
    "stats_api_port": "10085",
    "traffic_sample_ms": "1000",
    # opt-in: resolve segment DNS inside the gateway over DoH (for nodes that don't relay UDP)
    "dns_intercept": "0",
    # xray routing domainStrategy (AsIs | IPIfNonMatch | IPOnDemand) — how domain rules resolve
    "routing_domain_strategy": "IPIfNonMatch",
    # session idle timeout in minutes (0 = none) and daily auto-backup to data_dir/backups
    "session_timeout_min": "0",
    "auto_backup_enabled": "0",
    # IPv6 tunnel (off by default): carry segment client v6 through xray (static prefix; RA is
    # host-managed). segment_ip6 is the segment's static /64, informational + recommendation.
    "ipv6_enabled": "0",
    "segment_ip6": "",
    # self-provisioning gateway: the container owns the host segment + dnsmasq (+ opt DHCPv6-PD)
    "manage_segment": "1",
    "manage_dnsmasq": "1",
    "ipv6_pd": "0",
    "client_dns6": "2606:4700:4700::1111",
}
