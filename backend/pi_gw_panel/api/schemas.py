from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# Upper bounds so one request can't ship a multi-MB string (memory/CPU DoS — hashing a huge
# password burns scrypt CPU on unauthenticated /login; huge node fields bloat parse + DB + config).
_MAX_USER = 128
_MAX_PW = 256
_MAX_HOST = 253      # DNS name / SNI / address max
_MAX_FIELD = 512     # generic node string (note, path, alpn, keys)
_MAX_URL = 2048
_MAX_IMPORT = 512 * 1024   # a subscription/import blob
_MAX_BULK_IDS = 500
_MAX_RULES = 256
_MAX_NOISES = 32


class StrictIn(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NonNullPatch(StrictIn):
    """Optional means omittable, not explicitly nullable, except named FK fields."""

    nullable_fields: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null(cls, value):
        if isinstance(value, dict):
            bad = [key for key, item in value.items()
                   if item is None and key not in cls.nullable_fields]
            if bad:
                raise ValueError(f"fields may be omitted but not null: {', '.join(sorted(bad))}")
        return value


class LoginIn(StrictIn):
    username: str = Field(max_length=_MAX_USER)
    password: str = Field(max_length=_MAX_PW)


class SetupIn(StrictIn):
    username: str = Field(min_length=1, max_length=_MAX_USER)
    password: str = Field(min_length=8, max_length=_MAX_PW)   # SS1: minimum password length


class TokenCreateIn(StrictIn):
    name: str = Field(min_length=1, max_length=64)
    scope: Literal["monitor", "read", "readwrite"]
    expires_at: int | None = Field(default=None, ge=1)


class TokenOut(BaseModel):
    id: int
    name: str
    scope: str
    prefix: str
    created_at: int
    last_used_at: int | None = None
    expires_at: int | None = None


class TokenCreatedOut(TokenOut):
    token: str    # the full secret — returned ONCE at creation, never stored or shown again


class AuditEntryOut(BaseModel):
    ts: int
    actor: str    # "user:<name>" | "token:<prefix>" | "anon" (setup/login)
    method: str
    path: str
    status: int


class PasswordChangeIn(StrictIn):
    current_password: str = Field(max_length=_MAX_PW)
    new_password: str = Field(min_length=8, max_length=_MAX_PW)


class NodeIn(StrictIn):
    name: str = Field(max_length=_MAX_FIELD)
    address: str = Field(max_length=_MAX_HOST)
    port: int = Field(ge=1, le=65535)
    uuid: str = Field(max_length=_MAX_FIELD)
    transport: str = Field(default="vision", max_length=64)
    security: str = Field(default="reality", max_length=32)   # reality | tls (normalize() downgrades reality→tls if no key)
    sni: str = Field(default="", max_length=_MAX_HOST)
    public_key: str = Field(default="", max_length=_MAX_FIELD)
    short_id: str = Field(default="", max_length=_MAX_FIELD)
    fingerprint: str = Field(default="chrome", max_length=32)
    path: str = Field(default="", max_length=_MAX_FIELD)              # xhttp path
    host: str = Field(default="", max_length=_MAX_HOST)             # xhttp Host header
    mode: str = Field(default="", max_length=64)               # xhttp mode
    alpn: str = Field(default="", max_length=_MAX_FIELD)             # tls ALPN (comma-separated)
    note: str = Field(default="", max_length=_MAX_FIELD)             # free-text operator note / label


class NodeUpdate(NonNullPatch):
    nullable_fields = frozenset({"tuning_profile_id"})
    name: str | None = Field(default=None, max_length=_MAX_FIELD)
    address: str | None = Field(default=None, max_length=_MAX_HOST)
    port: int | None = Field(default=None, ge=1, le=65535)
    uuid: str | None = Field(default=None, max_length=_MAX_FIELD)
    transport: str | None = Field(default=None, max_length=64)
    security: str | None = Field(default=None, max_length=32)
    sni: str | None = Field(default=None, max_length=_MAX_HOST)
    public_key: str | None = Field(default=None, max_length=_MAX_FIELD)
    short_id: str | None = Field(default=None, max_length=_MAX_FIELD)
    fingerprint: str | None = Field(default=None, max_length=32)
    path: str | None = Field(default=None, max_length=_MAX_FIELD)
    host: str | None = Field(default=None, max_length=_MAX_HOST)
    mode: str | None = Field(default=None, max_length=64)
    alpn: str | None = Field(default=None, max_length=_MAX_FIELD)
    note: str | None = Field(default=None, max_length=_MAX_FIELD)
    tuning_profile_id: int | None = None


class NodeOut(BaseModel):
    id: int
    name: str
    address: str
    port: int
    uuid: str
    transport: str
    network: str = "tcp"
    security: str = "reality"
    sni: str = ""
    public_key: str = ""
    short_id: str = ""
    fingerprint: str = "chrome"
    path: str = ""
    host: str = ""
    mode: str = ""
    alpn: str = ""
    note: str = ""
    subscription_id: int | None = None
    stale: bool = False
    tuning_profile_id: int | None = None


class StatusOut(BaseModel):
    running: bool
    pid: int | None
    active_node_id: int | None
    xray_state: str = "stopped"   # working | stopped | error (sidebar xray-core box)
    active_since: int | None = None   # epoch the active node was applied (uptime anchor, P3)
    last_failover_at: float | None = None   # epoch of the last auto-failover (NN8)
    prev_active_node_id: int | None = None   # rollback target; None → Rollback button disabled (U2)
    server_now: float = 0.0   # Pi wall-clock at response time → client clock-skew correction (D4)
    tunnel_online: bool
    active_health_fresh: bool
    failover_ready: bool
    eligible_standby_count: int
    health_enabled: bool
    failover_enabled: bool
    failovers_24h: int


class SubscriptionIn(NonNullPatch):
    nullable_fields = frozenset({"default_profile_id"})
    name: str = Field(max_length=_MAX_FIELD)
    url: str = Field(max_length=_MAX_URL)
    interval_sec: int = 0
    injection: dict | None = Field(default=None, max_length=64)
    enabled: bool = True
    default_profile_id: int | None = None


class SubscriptionPatch(NonNullPatch):
    nullable_fields = frozenset({"default_profile_id"})
    name: str | None = Field(default=None, max_length=_MAX_FIELD)
    url: str | None = Field(default=None, max_length=_MAX_URL)
    interval_sec: int | None = None
    injection: dict | None = Field(default=None, max_length=64)
    enabled: bool | None = None
    default_profile_id: int | None = None


class SubscriptionOut(BaseModel):
    id: int
    name: str
    url: str
    injection: dict
    interval_sec: int
    enabled: bool
    default_profile_id: int | None
    last_fetched: str | None
    last_status: str | None
    last_path: str | None
    last_error: str | None
    up_bytes: int | None = None
    down_bytes: int | None = None
    total_bytes: int | None = None
    expire_at: int | None = None
    node_count: int


class SubscriptionRefreshOut(BaseModel):
    id: int
    name: str
    ok: bool
    status: str | None
    error: str | None


class RefreshAllOut(BaseModel):
    attempted: int
    succeeded: int
    failed: int
    results: list[SubscriptionRefreshOut]


class PreviewIn(StrictIn):
    url: str = Field(max_length=_MAX_URL)
    injection: dict | None = None


class PreviewOut(BaseModel):
    method: str
    url: str
    headers: dict
    query: dict


# N1: dry-run fetch+parse (no persist) — what nodes a sub WOULD yield, to catch a bad
# URL/token/format before saving.
class PreviewNodeOut(BaseModel):
    name: str
    address: str
    port: int
    transport: str
    network: str
    security: str


class PreviewNodesOut(BaseModel):
    format: str
    count: int
    returned_count: int = 0
    truncated: bool = False
    nodes: list[PreviewNodeOut]


# N4: import nodes from pasted subscription text (base64 / clash / json) as manual servers.
class ImportNodesIn(StrictIn):
    text: str = Field(max_length=_MAX_IMPORT)


class ImportNodesOut(BaseModel):
    added: int
    total: int
    format: str


# N8: reorder manual nodes (Servers tab) — position = list index.
class ReorderIn(StrictIn):
    ids: list[int] = Field(max_length=_MAX_BULK_IDS)


# NN3: bulk-detach nodes from their subscription (→ manual Servers).
class DetachIn(StrictIn):
    ids: list[int] = Field(max_length=_MAX_BULK_IDS)


class NodeValidateIn(NodeIn):
    tuning_profile_id: int | None = None


# NN10: pre-flight config validation for a node (xray -test) before connecting.
class NodeValidateOut(BaseModel):
    ok: bool
    error: str = ""


# N9: connect to the healthiest node in a scope (a subscription, or manual when null).
class ConnectBestIn(StrictIn):
    subscription_id: int | None = None


# Settings now carry tunneled-fetch + routing default + health/failover knobs. The
# anti-DPI tuning knobs moved to per-node tuning profiles (see ProfileIn/Out).
class SettingsOut(BaseModel):
    tunneled_fetch: bool
    routing_default_action: str
    health_enabled: bool
    health_interval: int
    health_hysteresis: int
    health_probe_url: str
    failover_enabled: bool
    failover_cooldown: int
    stats_enabled: bool
    stats_api_port: int
    traffic_sample_ms: int
    dns_intercept: bool
    session_timeout_min: int
    auto_backup_enabled: bool


class SettingsIn(NonNullPatch):
    tunneled_fetch: bool | None = None
    routing_default_action: str | None = None
    health_enabled: bool | None = None
    health_interval: int | None = None
    health_hysteresis: int | None = None
    health_probe_url: str | None = Field(default=None, max_length=_MAX_URL)
    failover_enabled: bool | None = None
    failover_cooldown: int | None = None
    stats_enabled: bool | None = None
    stats_api_port: int | None = None
    traffic_sample_ms: int | None = None
    dns_intercept: bool | None = None
    session_timeout_min: int | None = None
    auto_backup_enabled: bool | None = None


class DiagnosticsOut(BaseModel):
    app_version: str
    xray_version: str
    uptime_sec: int
    db_path: str
    db_bytes: int
    disk_free_bytes: int
    disk_total_bytes: int
    stats_last_ok_at: float | None = None
    stats_error: str = ""
    stats_fail_count: int = 0


# --- Wave 2: tuning profiles ---
class NoiseSpec(StrictIn):
    type: Literal["rand", "str", "base64", "hex"] = "rand"
    packet: str = Field(default="50-150", max_length=256)
    delay: str = Field(default="10-16", max_length=64)


class ProfileIn(StrictIn):
    name: str = Field(max_length=_MAX_FIELD)
    fingerprint: str = Field(default="chrome", max_length=32)
    frag_enabled: bool = False
    frag_packets: str = Field(default="tlshello", max_length=64)
    frag_length: str = Field(default="100-200", max_length=64)
    frag_interval: str = Field(default="10-20", max_length=64)
    mux_enabled: bool = False
    doh_enabled: bool = True
    doh_url: str = Field(default="", max_length=_MAX_URL)
    quic: Literal["allow", "drop", "proxy"] = "allow"
    noise_enabled: bool = False
    noises: list[NoiseSpec] = Field(default_factory=list, max_length=_MAX_NOISES)
    xhttp_padding: str = Field(default="", max_length=64)
    xmux_max_concurrency: str = Field(default="", max_length=64)
    xmux_max_connections: str = Field(default="", max_length=64)
    mux_concurrency: str = Field(default="", max_length=64)
    xudp_proxy_udp443: str = Field(default="", max_length=32)
    alpn: str = Field(default="", max_length=128)
    tls_min: str = Field(default="", max_length=16)
    tls_max: str = Field(default="", max_length=16)


class ProfileUpdate(NonNullPatch):
    name: str | None = Field(default=None, max_length=_MAX_FIELD)
    fingerprint: str | None = Field(default=None, max_length=32)
    frag_enabled: bool | None = None
    frag_packets: str | None = Field(default=None, max_length=64)
    frag_length: str | None = Field(default=None, max_length=64)
    frag_interval: str | None = Field(default=None, max_length=64)
    mux_enabled: bool | None = None
    doh_enabled: bool | None = None
    doh_url: str | None = Field(default=None, max_length=_MAX_URL)
    quic: Literal["allow", "drop", "proxy"] | None = None
    noise_enabled: bool | None = None
    noises: list[NoiseSpec] | None = Field(default=None, max_length=_MAX_NOISES)
    xhttp_padding: str | None = Field(default=None, max_length=64)
    xmux_max_concurrency: str | None = Field(default=None, max_length=64)
    xmux_max_connections: str | None = Field(default=None, max_length=64)
    mux_concurrency: str | None = Field(default=None, max_length=64)
    xudp_proxy_udp443: str | None = Field(default=None, max_length=32)
    alpn: str | None = Field(default=None, max_length=128)
    tls_min: str | None = Field(default=None, max_length=16)
    tls_max: str | None = Field(default=None, max_length=16)


class ProfileOut(BaseModel):
    id: int
    name: str
    fingerprint: str
    frag_enabled: bool
    frag_packets: str
    frag_length: str
    frag_interval: str
    mux_enabled: bool
    doh_enabled: bool
    doh_url: str
    quic: str
    noise_enabled: bool = False
    noises: list[NoiseSpec] = []
    xhttp_padding: str = ""
    xmux_max_concurrency: str = ""
    xmux_max_connections: str = ""
    mux_concurrency: str = ""
    xudp_proxy_udp443: str = ""
    alpn: str = ""
    tls_min: str = ""
    tls_max: str = ""
    is_default: bool = False
    is_active: bool = False        # this profile governs the live tunnel right now
    node_count: int = 0           # how many nodes use this profile explicitly


class ProfileValidateOut(BaseModel):
    ok: bool
    error: str = ""


class ProfilePresetInfo(BaseModel):
    name: str
    title: str
    fields: dict


class DefaultProfileIn(StrictIn):
    id: int


# --- Wave 2: routing ---
class RoutingRuleIn(StrictIn):
    type: Literal["geoip", "geosite", "domain", "ip", "port"]
    value: str = Field(max_length=_MAX_FIELD)
    action: Literal["direct", "proxy", "block"]
    enabled: bool = True
    label: str = Field(default="", max_length=_MAX_FIELD)


class RoutingRuleOut(BaseModel):
    id: int
    position: int
    type: str
    value: str
    action: str
    enabled: bool = True
    label: str = ""


class RoutingIn(StrictIn):
    rules: list[RoutingRuleIn] = Field(max_length=_MAX_RULES)
    default_action: Literal["direct", "proxy", "block"] = "proxy"
    domain_strategy: Literal["AsIs", "IPIfNonMatch", "IPOnDemand"] = "IPIfNonMatch"


class RoutingOut(BaseModel):
    rules: list[RoutingRuleOut]
    default_action: str
    domain_strategy: str = "IPIfNonMatch"


class RoutingValidateOut(BaseModel):
    ok: bool
    error: str = ""


class PresetInfo(BaseModel):
    name: str
    title: str


# --- Wave 2: per-node health snapshot ---
class NodeHealthOut(BaseModel):
    node_id: int
    last_tcp_ok: bool | None = None
    last_tcp_ms: int | None = None
    last_http_ok: bool | None = None
    last_http_ms: int | None = None
    last_real_ok: bool | None = None
    last_real_ms: int | None = None
    egress_ip: str | None = None
    egress_ip6: str | None = None
    egress_cc: str | None = None      # ISO-2 country of the egress (v4) — flag in the UI
    egress_cc6: str | None = None     # and v6
    checked_at: str | None = None
    fail_count: int = 0
    lat_history: list[int] = []


# --- Wave 3b: editable Pi network config + kill-switch + live status ---
class NetworkSegmentOut(BaseModel):
    iface: str
    ip: str
    ip6: str = ""           # segment IPv6 /64 (static CIDR / 'auto' for DHCPv6-PD / blank = auto-ULA)
    dhcp_start: str
    dhcp_end: str
    dhcp_lease: str
    client_dns: str
    client_dns6: str = "2606:4700:4700::1111"   # v6 DNS handed to clients via RA


class NetworkTunnelOut(BaseModel):
    real_ok: bool | None = None
    latency_ms: int | None = None
    egress_ip: str | None = None
    checked_at: str | None = None


class DhcpClientOut(BaseModel):
    ip: str
    mac: str
    hostname: str = ""
    expiry: int = 0


class NetworkStatusOut(BaseModel):
    segment_up: bool | None = None
    uplink: bool | None = None          # C1: Pi WAN/Home-leg reachability (None = unknown/dev)
    uplink6: bool | None = None         # G: IPv6 uplink reachability (only when v6 enabled)
    dhcp_clients: int = 0
    clients: list[DhcpClientOut] = []
    tunnel: NetworkTunnelOut
    wan_blocked: bool | None = None     # confirmed guard state; None = not yet verified / failed
    enforcement_status: Literal["ok", "unknown", "error"] = "unknown"
    enforcement_error: str = ""
    ipv6_prefix: str | None = None      # DHCPv6-PD 'auto': the host-delegated segment v6 prefix
    foreign_ra: bool | None = None      # Phase C: another router advertising v6 on the segment (leak)
    ipv6_prefix_source: str | None = None   # "static" | "ula" | "pd" — where the segment /64 came from


class RouterRecOut(BaseModel):
    title: str
    detail: str


class ConnEventOut(BaseModel):
    ts: int
    kind: str
    detail: str = ""


class NetworkOut(BaseModel):
    segment: NetworkSegmentOut
    kill_switch_enabled: bool
    lan_access_enabled: bool = True     # segment → home-LAN reachability (default on)
    ipv6_enabled: bool = False          # opt-in IPv6 tunnel
    status: NetworkStatusOut
    recommendations: list[RouterRecOut]
    events: list[ConnEventOut] = []     # N2: recent connection events (newest last)


# Partial, like SettingsIn: any provided editable field is set; empty strings are
# rejected (min_length=1), and unknown keys fail via NonNullPatch. The system knobs
# (tproxy port / marks / table) are intentionally not editable here.
class NetworkIn(NonNullPatch):
    segment_iface: str | None = Field(default=None, min_length=1, max_length=15)
    segment_ip: str | None = Field(default=None, min_length=1, max_length=15)
    dhcp_start: str | None = Field(default=None, min_length=1, max_length=15)
    dhcp_end: str | None = Field(default=None, min_length=1, max_length=15)
    dhcp_lease: str | None = Field(default=None, min_length=1, max_length=32)
    client_dns: str | None = Field(default=None, min_length=1, max_length=45)
    client_dns6: str | None = Field(default=None, min_length=1, max_length=45)
    segment_ip6: str | None = None          # empty allowed (clears the prefix / v6 off)
    kill_switch_enabled: bool | None = None
    lan_access_enabled: bool | None = None
    ipv6_enabled: bool | None = None


# Long-window traffic history seed for the Dashboard graph. Each sample is a compact
# [ts_ms, up_bps, down_bps] triple (proxy outbound); interval_ms is the record cadence.
class TrafficHistoryOut(BaseModel):
    samples: list[list[int]]
    interval_ms: int


class ReadinessChecksOut(BaseModel):
    provisioning: bool
    segment_addresses: bool
    dnsmasq: bool
    enforcement: bool
    active_node: bool
    xray: bool
    tunnel: bool


class ReadinessOut(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: ReadinessChecksOut
