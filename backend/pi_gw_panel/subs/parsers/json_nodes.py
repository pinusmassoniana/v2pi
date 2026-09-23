import json
from pi_gw_panel.models import PROTOCOLS, SS_METHODS, Node, ss_password_issue
from pi_gw_panel.subs.parsers import note_skip, safe_port, xray_json


def parse(body: str, *, limit: int | None = None, skipped: dict | None = None) -> list[Node]:
    return parse_obj(json.loads(body), limit=limit, skipped=skipped)


def parse_obj(data, *, limit: int | None = None, skipped: dict | None = None) -> list[Node]:
    """Build nodes from an already-decoded JSON object — lets the dispatcher hand over the
    object it parsed to sniff the format, avoiding a second json.loads on a 5MB body (P3)."""
    # A Remnawave XRAY_JSON feed is JSON too, but an array of whole client configs, not of nodes.
    if xray_json.looks_like(data):
        return xray_json.parse_obj(data, limit=limit, skipped=skipped)
    items = data if isinstance(data, list) else data.get("nodes", []) if isinstance(data, dict) else []
    nodes = []
    for it in items:
        # P2: skip non-dict / malformed entries so one bad item can't abort the whole feed
        # (matches the base64/clash parsers), instead of a KeyError/AttributeError propagating.
        if not isinstance(it, dict):
            note_skip(skipped, "invalid")
            continue
        try:
            addr = it.get("address")
            if not addr:
                note_skip(skipped, "invalid")
                continue
            # The boundary the other parsers and the backup document already hold: an unsupported
            # protocol is counted by name (Node.normalize used to turn it into VLESS), and an entry
            # without its protocol's credential is refused — stored, it failed every backup and
            # blocked every restore until someone deleted it.
            protocol = str(it.get("protocol") or "vless").strip().lower()
            if protocol not in PROTOCOLS:
                note_skip(skipped, protocol)
                continue
            uuid, password = str(it.get("uuid", "")), str(it.get("password", ""))
            method = str(it.get("method", "")).strip().lower()
            if (not uuid if protocol == "vless" else not password) or (
                    protocol == "shadowsocks"
                    and (method not in SS_METHODS or ss_password_issue(method, password))):
                note_skip(skipped, "invalid")
                continue
            net = str(it.get("network", ""))
            transport = it.get("transport") or ("xhttp" if net in ("xhttp", "splithttp") else "vision")
            pbk = str(it.get("public_key", ""))
            port_n = safe_port(it.get("port"))
            if port_n is None:
                note_skip(skipped, "invalid")
                continue
            nodes.append(Node(
                id=None, name=str(it.get("name", addr)),
                address=str(addr), port=port_n,
                uuid=uuid, protocol=protocol, password=password, method=method,
                transport=transport,
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
            note_skip(skipped, "invalid")
            continue
    return nodes
