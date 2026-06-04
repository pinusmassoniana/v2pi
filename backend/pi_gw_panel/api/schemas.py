from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str
    password: str


class SetupIn(BaseModel):
    username: str
    password: str


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str


class NodeIn(BaseModel):
    name: str
    address: str
    port: int
    uuid: str
    transport: str = "vision"
    sni: str = ""
    public_key: str = ""
    short_id: str = ""
    fingerprint: str = "chrome"


class NodeUpdate(BaseModel):
    name: str | None = None
    address: str | None = None
    port: int | None = None
    uuid: str | None = None
    transport: str | None = None
    sni: str | None = None
    public_key: str | None = None
    short_id: str | None = None
    fingerprint: str | None = None
    tuning_profile_id: int | None = None


class NodeOut(BaseModel):
    id: int
    name: str
    address: str
    port: int
    uuid: str
    transport: str
    sni: str = ""
    public_key: str = ""
    short_id: str = ""
    fingerprint: str = "chrome"
    subscription_id: int | None = None
    stale: bool = False
    tuning_profile_id: int | None = None


class StatusOut(BaseModel):
    running: bool
    pid: int | None
    active_node_id: int | None
    xray_state: str = "stopped"   # working | stopped | error (sidebar xray-core box)


class SubscriptionIn(BaseModel):
    name: str
    url: str
    interval_sec: int = 0
    injection: dict | None = None


class SubscriptionPatch(BaseModel):
    name: str | None = None
    url: str | None = None
    interval_sec: int | None = None
    injection: dict | None = None


class SubscriptionOut(BaseModel):
    id: int
    name: str
    url: str
    injection: dict
    interval_sec: int
    last_fetched: str | None
    last_status: str | None
    last_path: str | None
    node_count: int


class PreviewIn(BaseModel):
    url: str
    injection: dict | None = None


class PreviewOut(BaseModel):
    method: str
    url: str
    headers: dict
    query: dict


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


class SettingsIn(BaseModel):
    tunneled_fetch: bool | None = None
    routing_default_action: str | None = None
    health_enabled: bool | None = None
    health_interval: int | None = None
    health_hysteresis: int | None = None
    health_probe_url: str | None = None
    failover_enabled: bool | None = None
    failover_cooldown: int | None = None
    stats_enabled: bool | None = None
    stats_api_port: int | None = None
    traffic_sample_ms: int | None = None


# --- Wave 2: tuning profiles ---
class ProfileIn(BaseModel):
    name: str
    fingerprint: str = "chrome"
    frag_enabled: bool = False
    frag_packets: str = "tlshello"
    frag_length: str = "100-200"
    frag_interval: str = "10-20"
    mux_enabled: bool = False
    doh_enabled: bool = True
    doh_url: str = ""
    quic: str = "allow"


class ProfileUpdate(BaseModel):
    name: str | None = None
    fingerprint: str | None = None
    frag_enabled: bool | None = None
    frag_packets: str | None = None
    frag_length: str | None = None
    frag_interval: str | None = None
    mux_enabled: bool | None = None
    doh_enabled: bool | None = None
    doh_url: str | None = None
    quic: str | None = None


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
    is_default: bool = False


class DefaultProfileIn(BaseModel):
    id: int


# --- Wave 2: routing ---
class RoutingRuleIn(BaseModel):
    type: str    # geoip | geosite | domain | ip | port
    value: str
    action: str  # direct | proxy | block


class RoutingRuleOut(BaseModel):
    id: int
    position: int
    type: str
    value: str
    action: str


class RoutingIn(BaseModel):
    rules: list[RoutingRuleIn]
    default_action: str = "proxy"


class RoutingOut(BaseModel):
    rules: list[RoutingRuleOut]
    default_action: str


# --- Wave 2: per-node health snapshot ---
class NodeHealthOut(BaseModel):
    node_id: int
    last_tcp_ok: bool | None = None
    last_tcp_ms: int | None = None
    last_real_ok: bool | None = None
    last_real_ms: int | None = None
    egress_ip: str | None = None
    checked_at: str | None = None
    fail_count: int = 0


# --- Wave 3b: editable Pi network config + kill-switch + live status ---
class NetworkSegmentOut(BaseModel):
    iface: str
    ip: str
    dhcp_start: str
    dhcp_end: str
    dhcp_lease: str
    client_dns: str


class NetworkTunnelOut(BaseModel):
    real_ok: bool | None = None
    latency_ms: int | None = None
    egress_ip: str | None = None


class NetworkStatusOut(BaseModel):
    segment_up: bool | None = None
    dhcp_clients: int = 0
    tunnel: NetworkTunnelOut


class RouterRecOut(BaseModel):
    title: str
    detail: str


class NetworkOut(BaseModel):
    segment: NetworkSegmentOut
    kill_switch_enabled: bool
    status: NetworkStatusOut
    recommendations: list[RouterRecOut]


# Partial, like SettingsIn: any provided editable field is set; empty strings are
# rejected (min_length=1), unknown keys ignored (pydantic default). The system knobs
# (tproxy port / marks / table) are intentionally not editable here.
class NetworkIn(BaseModel):
    segment_iface: str | None = Field(default=None, min_length=1)
    segment_ip: str | None = Field(default=None, min_length=1)
    dhcp_start: str | None = Field(default=None, min_length=1)
    dhcp_end: str | None = Field(default=None, min_length=1)
    dhcp_lease: str | None = Field(default=None, min_length=1)
    client_dns: str | None = Field(default=None, min_length=1)
    kill_switch_enabled: bool | None = None
