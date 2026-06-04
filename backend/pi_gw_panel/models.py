from dataclasses import dataclass, field
from typing import Literal


@dataclass
class Node:
    id: int | None
    name: str
    address: str
    port: int
    uuid: str
    transport: Literal["vision", "xhttp"] = "vision"
    sni: str = ""
    public_key: str = ""
    short_id: str = ""
    fingerprint: str = "chrome"
    flow: str = "xtls-rprx-vision"
    network: str = "tcp"        # tcp | xhttp
    security: str = "reality"   # reality | tls
    path: str = ""              # xhttp path
    host: str = ""              # xhttp Host header
    mode: str = ""              # xhttp mode (e.g. stream-up)
    alpn: str = ""              # tls ALPN (comma-separated)
    subscription_id: int | None = None
    stale: bool = False
    tuning_profile_id: int | None = None
    position: int = 0           # order within its subscription (set on reconcile)

    def __post_init__(self) -> None:
        # `flow` (xtls-rprx-vision) is Vision-only; XHTTP nodes must not carry it.
        if self.transport != "vision":
            self.flow = ""


@dataclass
class Subscription:
    id: int | None
    name: str
    url: str
    injection: dict = field(default_factory=dict)
    interval_sec: int = 0
    last_fetched: str | None = None
    last_status: str | None = None
    last_path: str | None = None


@dataclass
class TuningProfile:
    id: int | None
    name: str
    fingerprint: str = "chrome"
    frag_enabled: bool = False
    frag_packets: str = "tlshello"
    frag_length: str = "100-200"
    frag_interval: str = "10-20"
    mux_enabled: bool = False
    doh_enabled: bool = True
    doh_url: str = ""
    quic: str = "allow"  # allow | drop | proxy


@dataclass
class RoutingRule:
    id: int | None
    position: int
    type: str   # geoip | geosite | domain | ip | port
    value: str
    action: str  # direct | proxy | block


@dataclass
class NodeHealth:
    node_id: int
    last_tcp_ok: bool | None = None
    last_tcp_ms: int | None = None
    last_real_ok: bool | None = None
    last_real_ms: int | None = None
    egress_ip: str | None = None
    checked_at: str | None = None
    fail_count: int = 0
