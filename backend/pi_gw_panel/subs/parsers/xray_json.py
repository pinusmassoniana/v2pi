"""Remnawave's XRAY_JSON subscription format: an array of complete Xray client configs, one per
server, each carrying the server as its `proxy` outbound and its display name in `remarks`.

It is what a Remnawave panel sends Happ and v2rayNG when its operator turns on "Serve JSON at Base
Subscription" (`JSON_SUBSCRIPTION_FALLBACK_CLIENTS` in its backend), so a header preset imitating
either client has to be able to read it — the providers that care which client is asking are the
ones most likely to answer this way.

Only the proxy outbound is taken. Routing, DNS and the rest of each config are the provider's
choices for a phone; this gateway has its own.
"""
from pi_gw_panel.models import SS_METHODS, Node, ss_password_issue
from pi_gw_panel.subs.parsers import note_skip, safe_port

_PROXY_PROTOCOLS = ("vless", "trojan", "shadowsocks")
# streamSettings.network → the panel's transport. Anything absent (ws, grpc, httpupgrade, kcp) is a
# transport the panel does not build, so such a server is refused rather than imported as TCP.
_TRANSPORTS = {"tcp": "vision", "raw": "vision", "xhttp": "xhttp", "splithttp": "xhttp"}


def looks_like(data) -> bool:
    """A config (or a list of them) with an `outbounds` list — what no other JSON feed has."""
    items = data if isinstance(data, list) else [data]
    head = items[:3]
    return bool(head) and all(isinstance(i, dict) and isinstance(i.get("outbounds"), list)
                              for i in head)


def parse_obj(data, *, limit: int | None = None, skipped: dict | None = None) -> list[Node]:
    nodes: list[Node] = []
    for config in data if isinstance(data, list) else [data]:
        outbound = _proxy_outbound(config) if isinstance(config, dict) else None
        if outbound is None:
            note_skip(skipped, "invalid")
            continue
        if outbound.get("protocol") not in _PROXY_PROTOCOLS:
            note_skip(skipped, outbound.get("protocol"))     # hysteria and friends, by name
            continue
        node = _node(config, outbound)
        if node is None:
            note_skip(skipped, "invalid")
            continue
        nodes.append(node)
        if limit is not None and len(nodes) >= limit:
            break
    return nodes


def _proxy_outbound(config: dict) -> dict | None:
    outbounds = [o for o in config.get("outbounds") or [] if isinstance(o, dict)]
    tagged = next((o for o in outbounds if o.get("tag") == "proxy"), None)
    if tagged is not None:
        return tagged
    # No `proxy` tag: the first outbound that is a server at all, never `direct` or `block`.
    return next((o for o in outbounds if o.get("protocol") not in ("freedom", "blackhole", "dns")),
                None)


def _first(value) -> dict | None:
    return value[0] if isinstance(value, list) and value and isinstance(value[0], dict) else None


def _node(config: dict, outbound: dict) -> Node | None:
    protocol = outbound["protocol"]
    settings = outbound.get("settings") if isinstance(outbound.get("settings"), dict) else {}
    stream = outbound.get("streamSettings") if isinstance(outbound.get("streamSettings"), dict) else {}

    if protocol == "vless":
        server = _first(settings.get("vnext"))
        user = _first(server.get("users")) if server else None
        if server is None or user is None or not user.get("id"):
            return None
        credential = {"uuid": str(user["id"]), "flow": str(user.get("flow") or "")}
    else:
        server = _first(settings.get("servers"))
        if server is None or not server.get("password"):
            return None
        credential = {"uuid": "", "password": str(server["password"])}
        if protocol == "shadowsocks":
            method = str(server.get("method") or "").strip().lower()
            if method not in SS_METHODS or ss_password_issue(method, credential["password"]):
                return None
            credential["method"] = method

    address = str(server.get("address") or "")
    port = safe_port(server.get("port"))
    if not address or port is None:
        return None
    transport = _TRANSPORTS.get(str(stream.get("network") or "tcp").lower())
    if transport is None or (protocol != "vless" and transport != "vision"):
        return None

    reality = stream.get("realitySettings") if isinstance(stream.get("realitySettings"), dict) else {}
    tls = stream.get("tlsSettings") if isinstance(stream.get("tlsSettings"), dict) else {}
    xhttp = next((stream[k] for k in ("xhttpSettings", "splithttpSettings")
                  if isinstance(stream.get(k), dict)), {})
    alpn = tls.get("alpn")
    return Node(
        id=None, name=str(config.get("remarks") or address), address=address, port=port,
        protocol=protocol, **credential,
        transport=transport, network="xhttp" if transport == "xhttp" else "tcp",
        # Node.normalize() re-derives this per protocol and never honours plaintext for VLESS.
        security=str(stream.get("security") or ""),
        sni=str(reality.get("serverName") or tls.get("serverName") or ""),
        # Newer Xray also accepts the REALITY public key as `password`.
        public_key=str(reality.get("publicKey") or reality.get("password") or ""),
        short_id=str(reality.get("shortId") or ""),
        fingerprint=str(reality.get("fingerprint") or tls.get("fingerprint") or "chrome"),
        alpn=",".join(str(a) for a in alpn) if isinstance(alpn, list) else str(alpn or ""),
        path=str(xhttp.get("path") or ""), host=str(xhttp.get("host") or ""),
        mode=str(xhttp.get("mode") or ""),
    )
