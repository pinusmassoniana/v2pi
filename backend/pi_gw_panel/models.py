import base64
import binascii
from dataclasses import dataclass, field
from typing import Literal


# B4: what a node speaks. VLESS is the original and the default; Trojan and Shadowsocks were
# added because a mixed subscription carries them and the panel used to drop those entries.
# Hysteria2 is NOT here — xray 26.3.27 refuses the outbound outright ("unknown config id:
# hysteria2"), so offering it would be a node that can never connect.
PROTOCOLS = ("vless", "trojan", "shadowsocks")

# The Shadowsocks ciphers xray 26.3.27 builds an outbound for, minus `none` (no encryption at
# all — the same reason `security=none` is downgraded below). Verified against the pinned
# binary; `rc4-md5` and friends are refused by it, so they are refused here.
SS_METHODS = ("aes-128-gcm", "aes-256-gcm", "chacha20-poly1305", "chacha20-ietf-poly1305",
              "xchacha20-poly1305", "2022-blake3-aes-128-gcm", "2022-blake3-aes-256-gcm",
              "2022-blake3-chacha20-poly1305")
DEFAULT_SS_METHOD = "aes-128-gcm"

# The 2022 ciphers take a base64 key of an exact length, and xray refuses to START on a wrong
# one — not at config-build time. Left to xray that is a failed apply with a base64 error in the
# log; caught here it is a sentence next to the field the key was typed into.
_SS2022_KEY_BYTES = {"2022-blake3-aes-128-gcm": 16, "2022-blake3-aes-256-gcm": 32,
                     "2022-blake3-chacha20-poly1305": 32}


def ss_password_issue(method: str, password: str) -> str | None:
    """Why this password cannot be used with this cipher, or None. Only the 2022 ciphers
    constrain it; the older ones take any string."""
    needed = _SS2022_KEY_BYTES.get(method)
    if needed is None:
        return None
    try:
        key = base64.b64decode(password, validate=True)
    except (binascii.Error, ValueError):
        return f"{method} needs a base64-encoded {needed}-byte key"
    if len(key) != needed:
        return f"{method} needs a {needed}-byte key ({len(key)} bytes decoded)"
    return None


@dataclass
class Node:
    id: int | None
    name: str
    address: str
    port: int
    uuid: str
    protocol: str = "vless"     # vless | trojan | shadowsocks
    password: str = ""          # trojan/shadowsocks credential (vless uses `uuid`)
    method: str = ""            # shadowsocks cipher
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
    note: str = ""              # free-text operator note / label (searchable)
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
        if self.protocol not in PROTOCOLS:
            self.protocol = "vless"          # an unknown protocol is never rendered into a config
        if self.protocol != "vless":
            # Trojan and Shadowsocks carry a password, not a uuid, and neither takes a Vision
            # flow or XHTTP: the panel builds both as plain TCP. Clearing the XHTTP fields here
            # (rather than leaving them) keeps a node converted from VLESS from carrying a path
            # and Host that nothing reads.
            self.transport = "vision"
            self.network = "tcp"
            self.flow = ""
            self.path = self.host = self.mode = ""
            if self.protocol == "shadowsocks":
                # Its own cipher end to end — no TLS layer to configure, and an unknown method
                # is not silently corrected: the builder refuses it where it is visible.
                self.security = "none"
                self.method = self.method or DEFAULT_SS_METHOD
                return
            # Trojan keeps the VLESS reality/tls rule below — xray builds trojan over either.
        elif self.transport == "xhttp":
            self.network = "xhttp"
            self.flow = ""                       # Vision-only flow; XHTTP carries none
        else:                                    # vision
            self.network = "tcp"
            if not self.flow:
                self.flow = "xtls-rprx-vision"
        # `security` arrives from untrusted subscription feeds and goes straight into
        # streamSettings.security. Only reality/tls are ever honoured — a feed offering
        # `security=none` (plaintext VLESS) is downgraded here, not passed through.
        if self.security not in ("reality", "tls"):
            self.security = "reality" if self.public_key else "tls"
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
    # What the last refresh DROPPED, per protocol ({"trojan": 3, "invalid": 1}): the panel is
    # VLESS-only, so a mixed feed silently yields fewer nodes than it lists without this.
    last_skipped: dict = field(default_factory=dict)


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
    # v1.4 anti-DPI additions
    noise_enabled: bool = False
    noises: list = field(default_factory=list)   # [{type, packet, delay}] on the fragment outbound
    xhttp_padding: str = ""          # xhttpSettings.extra.xPaddingBytes (xhttp nodes)
    xmux_max_concurrency: str = ""   # xhttpSettings.extra.xmux.maxConcurrency
    xmux_max_connections: str = ""   # xhttpSettings.extra.xmux.maxConnections
    mux_concurrency: str = ""        # mux.concurrency (non-Vision only)
    xudp_proxy_udp443: str = ""      # mux.xudpProxyUDP443: "" | reject | allow | skip
    alpn: str = ""                   # tlsSettings.alpn (tls-mode nodes), comma-separated
    tls_min: str = ""                # tlsSettings.minVersion
    tls_max: str = ""                # tlsSettings.maxVersion


@dataclass
class RoutingRule:
    id: int | None
    position: int
    type: str   # geoip | geosite | domain | ip | port | device
    value: str
    action: str  # direct | proxy | block
    enabled: bool = True
    label: str = ""
    # A3: which geo dataset a geoip/geosite rule reads. "" is the stock files under xray's own
    # names (geoip:ru); "ru" is runetfreedom's, addressed as ext:geosite_ru.dat:ru-blocked.
    # Ignored for domain/ip/port rules, which carry literals.
    dataset: str = ""


@dataclass
class Reservation:
    """A4: a device whose IP the segment's DHCP always hands back, so it can be named — and
    routed and counted — by something stabler than "whatever it got this time"."""
    id: int | None
    mac: str          # lowercase aa:bb:cc:dd:ee:ff
    ip: str           # inside the segment /24, never the gateway's own address
    name: str         # hostname charset; dnsmasq hands it out and the UI shows it
    created_at: int = 0


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
    egress_ip6: str | None = None         # IPv6 egress (when the node carries v6); None if no v6
    checked_at: str | None = None
    fail_count: int = 0
    lat_history: list[int] = field(default_factory=list)   # recent HTTPS-handshake latencies (NN4)
