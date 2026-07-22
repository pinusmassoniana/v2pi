import json
from pi_gw_panel.models import Node
from pi_gw_panel.subs.parsers import safe_port


def parse(body: str, *, limit: int | None = None) -> list[Node]:
    return parse_obj(json.loads(body), limit=limit)


def parse_obj(data, *, limit: int | None = None) -> list[Node]:
    """Build nodes from an already-decoded JSON object — lets the dispatcher hand over the
    object it parsed to sniff the format, avoiding a second json.loads on a 5MB body (P3)."""
    items = data if isinstance(data, list) else data.get("nodes", []) if isinstance(data, dict) else []
    nodes = []
    for it in items:
        # P2: skip non-dict / malformed entries so one bad item can't abort the whole feed
        # (matches the base64/clash parsers), instead of a KeyError/AttributeError propagating.
        if not isinstance(it, dict):
            continue
        try:
            addr = it.get("address")
            if not addr:
                continue
            net = str(it.get("network", ""))
            transport = it.get("transport") or ("xhttp" if net in ("xhttp", "splithttp") else "vision")
            pbk = str(it.get("public_key", ""))
            port_n = safe_port(it.get("port"))
            if port_n is None:
                continue
            nodes.append(Node(
                id=None, name=str(it.get("name", addr)),
                address=str(addr), port=port_n,
                uuid=str(it.get("uuid", "")), transport=transport,
                network=net or ("xhttp" if transport == "xhttp" else "tcp"),
                security=str(it.get("security", "")) or ("reality" if pbk else "tls"),
                sni=str(it.get("sni", "")), public_key=pbk,
                short_id=str(it.get("short_id", "")),
                fingerprint=str(it.get("fingerprint", "chrome")),
                flow=str(it.get("flow", "xtls-rprx-vision")),
                path=str(it.get("path", "")), host=str(it.get("host", "")),
                mode=str(it.get("mode", "")), alpn=str(it.get("alpn", "")),
            ))
            if limit is not None and len(nodes) >= limit:
                break
        except (KeyError, TypeError, ValueError):
            continue
    return nodes
