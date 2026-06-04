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
        self.normalize()

    def normalize(self) -> None:
        """Single source of truth for the transport↔network↔security↔flow invariants.

        ``transport`` is the UI-facing choice; ``network`` + ``security`` + ``flow`` are what
        the xray config builder consumes. Keeping them coherent here — and calling this after
        every edit (see ``update_node``) — is what stops parsers and manual add/edit from
        drifting (the old bug class: an xhttp node built as tcp, or reality with no key).

        Idempotent: safe to run on DB reads and repeated edits.
        """
        if self.transport == "xhttp":
            self.network = "xhttp"
            self.flow = ""                       # Vision-only flow; XHTTP carries none
        else:                                    # vision
            self.network = "tcp"
            if not self.flow:
                self.flow = "xtls-rprx-vision"
        # reality needs a public key; without one fall back to plain TLS (a reality
        # outbound with an empty publicKey is broken/insecure).
        if self.security == "reality" and not self.public_key:
            self.security = "tls"


@dataclass
class Subscription:
    id: int | None
    name: str
    url: str
    injection: dict = field(default_factory=dict)
    interval_sec: int = 0
    enabled: bool = True                  # N2: pause auto + manual refresh without deleting
    default_profile_id: int | None = None  # N5: tuning profile new reconciled nodes inherit
    last_fetched: str | None = None
    last_status: str | None = None
    last_path: str | None = None
    last_error: str | None = None         # N6: full last error text (status stays a short line)
    # N7: Subscription-Userinfo quota/expiry, when the provider sends the header
    up_bytes: int | None = None
    down_bytes: int | None = None
    total_bytes: int | None = None
    expire_at: int | None = None          # epoch seconds; 0/None = no expiry


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
    last_http_ok: bool | None = None      # direct HTTPS-handshake (all nodes)
    last_http_ms: int | None = None
    last_real_ok: bool | None = None      # real request through the node (active / on-demand)
    last_real_ms: int | None = None
    egress_ip: str | None = None
    checked_at: str | None = None
    fail_count: int = 0
    lat_history: list[int] = field(default_factory=list)   # recent HTTPS-handshake latencies (NN4)
