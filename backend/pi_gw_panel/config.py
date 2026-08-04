import logging
import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger(__name__)
_IFACE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")


def safe_int(value, default: int, name: str = "value") -> int:
    """int(value) but falling back to `default` (with a warning) on a bad/None value, so a typo
    or stray newline in an env var — or a corrupt cosmetic DB setting — can't crash boot."""
    try:
        return int(value)
    except (TypeError, ValueError):
        _log.warning("invalid int for %s: %r — using default %d", name, value, default)
        return default


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
    tls_cert: str = ""
    tls_key: str = ""
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
        os.makedirs(self.data_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError as exc:
            _log.warning("could not secure data directory %s: %s", self.data_dir, exc)
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
        secret = env.get("PI_GW_SESSION_SECRET", "")
        if secret and len(secret.encode("utf-8")) < 32:
            raise ValueError("PI_GW_SESSION_SECRET must be at least 32 bytes")
        tls_cert = env.get("PI_GW_TLS_CERT", "")
        tls_key = env.get("PI_GW_TLS_KEY", "")
        if bool(tls_cert) != bool(tls_key):
            raise ValueError("PI_GW_TLS_CERT and PI_GW_TLS_KEY must be configured together")
        values = dict(
            data_dir=data,
            db_path=os.path.join(data, "pi_gw_panel.sqlite"),
            config_path=os.path.join(data, "xray.json"),
            lastgood_path=os.path.join(data, "xray.lastgood.json"),
            bind_host=env.get("PI_GW_BIND_HOST", "0.0.0.0"),   # reachable by default; auth-gated
            tls_cert=tls_cert,
            tls_key=tls_key,
            static_dir=env.get("PI_GW_STATIC_DIR", _packaged_static()),
            xray_bin=env.get("PI_GW_XRAY_BIN", "xray"),
            session_secret=secret,
            # safe-int so a typo/stray newline in the env var can't crash boot (audit P2)
            login_lockout_sec=safe_int(env.get("PI_GW_LOGIN_LOCKOUT_SEC", "60"), 60,
                                       "PI_GW_LOGIN_LOCKOUT_SEC"),
            dnsmasq_leases=env.get("PI_GW_DNSMASQ_LEASES", os.path.join(data, "dnsmasq.leases")),
            client_dns6=env.get("PI_GW_CLIENT_DNS6", "2606:4700:4700::1111"),
            geoip_db=env.get("PI_GW_GEOIP_DB", "/usr/local/share/dbip-country-lite.mmdb"),
            mgmt_iface=env.get("PI_GW_MGMT_IFACE", "eth0"),
            mgmt_ip=env.get("PI_GW_MGMT_IP", "192.168.1.120"),
            segment_iface=env.get("PI_GW_SEGMENT_IFACE", "eth0.2"),
            segment_ip=env.get("PI_GW_SEGMENT_IP", "192.168.10.2"),
            dhcp_start=env.get("PI_GW_DHCP_START", "192.168.10.30"),
            dhcp_end=env.get("PI_GW_DHCP_END", "192.168.10.200"),
            dhcp_lease=env.get("PI_GW_DHCP_LEASE", "12h"),
            client_dns=env.get("PI_GW_CLIENT_DNS", "1.1.1.1"),
        )
        for key in ("mgmt_iface", "segment_iface"):
            if not _IFACE_RE.fullmatch(values[key]):
                raise ValueError(f"invalid interface name for {key}")
        for key in ("mgmt_ip", "segment_ip", "dhcp_start", "dhcp_end"):
            try:
                ipaddress.IPv4Address(values[key])
            except ValueError as exc:
                raise ValueError(f"{key} must be an IPv4 address") from exc
        try:
            ipaddress.ip_address(values["client_dns"])
        except ValueError as exc:
            raise ValueError("client_dns must be an IP address") from exc
        if not re.fullmatch(r"[1-9][0-9]*[smhdw]", values["dhcp_lease"]):
            raise ValueError("dhcp_lease must be a positive duration such as 12h")
        return cls(**values)

    @property
    def bootstrap_token_path(self) -> str:
        return os.path.join(self.data_dir, "bootstrap_token")

    @property
    def tls_enabled(self) -> bool:
        return bool(self.tls_cert and self.tls_key)

    @property
    def loopback_bind(self) -> bool:
        if self.bind_host.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.bind_host).is_loopback
        except ValueError:
            return False

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
    # When a single-server subscription rotates its identity, a scheduled refresh may move the
    # LIVE tunnel onto the replacement the feed just supplied. That is unattended, remote-driven
    # config, so it is switchable — and even when on it never accepts weaker node security.
    "subs_auto_switch": "1",
    "routing_default_action": "proxy",
    # Master switch: off stops BOTH health loops (kept as-is so an install that turned health
    # off stays off after upgrade).
    "health_enabled": "1",
    # The two loops it covers are separately controllable, because they cost very different
    # things: the sweep touches every node in the pool, while the active check touches one.
    #   sweep  — TCP + direct-HTTPS across ALL nodes, every `health_interval`
    #   active — a real request through the ACTIVE node, every `health_active_interval`
    # Failover reads the active check's fail_count, so turning the sweep off leaves failover intact.
    "health_sweep_enabled": "1",
    "health_interval": "1800",
    "health_active_interval": "60",
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


# --- shared value validation -----------------------------------------------------------------
# ONE validator per family, used by every path that can write these settings: the interactive
# routes (which turn the ValueError into a 422) and a restored backup document. A second copy is
# exactly how restore came to accept a `dhcp_lease` carrying a newline while `PUT /api/network`
# rejected it — and these values are interpolated verbatim into the dnsmasq config the panel
# starts as root, where an embedded newline is a new directive (`dhcp-script=…`), not data.

# IFNAMSIZ-bounded, and only characters that are inert in nft/dnsmasq syntax.
_NET_IFACE_RE = re.compile(r"[A-Za-z0-9._@:-]{1,15}")
# dnsmasq lease time: '3600', '45m', '12h', '2d', '1w', 'infinite'.
_NET_LEASE_RE = re.compile(r"infinite|\d{1,9}[smhdw]?")

# Segment/DHCP/DNS settings. Rendered into nft + dnsmasq; every one of them is operator-settable
# through PUT /api/network AND through a restored backup.
NET_SETTING_KEYS = ("segment_iface", "segment_ip", "segment_ip6", "dhcp_start", "dhcp_end",
                    "dhcp_lease", "client_dns", "client_dns6", "ula_prefix6")
_NET_IPV4_KEYS = ("segment_ip", "dhcp_start", "dhcp_end", "client_dns")


def validate_net_settings(data: Mapping[str, Any]) -> dict[str, str]:
    """Check every segment/DHCP/DNS setting present in `data`; return the normalized values.

    Raises ``ValueError('<field>: <why>')``. An empty value is left alone — it means "fall back
    to the configured default" everywhere these settings are read. Matching is done with
    ``fullmatch`` on purpose: ``re.match(r'…$', 'eth0\\n')`` succeeds, so an anchored ``$``
    would still let a trailing newline through into the rendered config.
    """
    def bad(field: str, why: str):
        raise ValueError(f"{field}: {why}")

    def present(field: str) -> str | None:
        if field not in data:
            return None
        value = str(data[field] if data[field] is not None else "").strip()
        return value or None

    out: dict[str, str] = {}
    if "segment_iface" in data:
        value = present("segment_iface")
        if not value or not _NET_IFACE_RE.fullmatch(value):
            bad("segment_iface", "must be a plain interface name")
        out["segment_iface"] = value
    for field in _NET_IPV4_KEYS:
        value = present(field)
        if value is None:
            continue
        try:
            ipaddress.IPv4Address(value)
        except ValueError:
            bad(field, "must be an IPv4 address")
        out[field] = value
    value = present("client_dns6")
    if value is not None:
        try:
            ipaddress.IPv6Address(value)
        except ValueError:
            bad("client_dns6", "must be an IPv6 address")
        out["client_dns6"] = value
    value = present("dhcp_lease")
    if value is not None:
        if not _NET_LEASE_RE.fullmatch(value):
            bad("dhcp_lease", "must be a lease time like '12h', '3600', or 'infinite'")
        out["dhcp_lease"] = value
    value = present("segment_ip6")
    if value is not None and value.lower() != "auto":
        out["segment_ip6"] = _v6_prefix64(value, "segment_ip6", "an IPv6 /64 or 'auto'")
    elif value is not None:
        out["segment_ip6"] = value
    value = present("ula_prefix6")
    if value is not None:
        out["ula_prefix6"] = _v6_prefix64(value, "ula_prefix6", "an IPv6 /64 prefix")
    return out


def _v6_prefix64(value: str, field: str, what: str) -> str:
    try:
        network = ipaddress.IPv6Network(value, strict=False)
    except ValueError as exc:
        raise ValueError(f"{field}: must be {what}") from exc
    if network.prefixlen != 64:
        raise ValueError(f"{field}: must use a /64 prefix")
    return str(network)


# Settings whose stored value is `int()`-ed on read, with the bounds the runtime needs. A restore
# that stored a non-numeric one used to make GET /api/settings raise for good, and kill the
# health sweep with it.
SETTINGS_INT_BOUNDS: dict[str, tuple[int, int | None]] = {
    # health_active_interval spins up a throwaway xray each time, so its floor is well above
    # zero — a few seconds would mean a permanent probe process.
    "health_interval": (60, None),
    "health_active_interval": (10, None),
    "health_hysteresis": (1, None),
    "failover_cooldown": (0, None),
    "session_timeout_min": (0, None),
    "traffic_sample_ms": (500, 60_000),
    "stats_api_port": (1, 65535),
}
# Settings that feed straight into the built xray config: an out-of-set value produces a config
# xray rejects on the next apply (a self-inflicted outage).
SETTINGS_CHOICES: dict[str, tuple[str, ...]] = {
    "routing_default_action": ("direct", "proxy", "block"),
    "routing_domain_strategy": ("AsIs", "IPIfNonMatch", "IPOnDemand"),
}


def validate_setting_values(data: Mapping[str, Any], *, reserved_ports: tuple[int, ...] = ()) -> None:
    """Type/range-check the settings present in `data` (int-typed keys and closed choices).

    Accepts both the ints the API sends and the strings a backup carries. Raises ValueError.
    """
    for key, (low, high) in SETTINGS_INT_BOUNDS.items():
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, bool):
            raise ValueError(f"{key} must be an integer")
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be an integer") from None
        if key == "stats_api_port":
            if not low <= number <= high:
                raise ValueError("stats_api_port must be 1..65535")
            if number in reserved_ports:
                raise ValueError("stats_api_port collides with a system port")
            continue
        if number < low:
            raise ValueError(f"{key} must be >= {low}")
        if high is not None and number > high:
            raise ValueError(f"{key} must be <= {high}")
    for key, allowed in SETTINGS_CHOICES.items():
        if key in data and str(data[key]) not in allowed:
            raise ValueError(f"{key} must be {'/'.join(allowed)}")
