import base64
import binascii
import urllib.parse
from pi_gw_panel.models import Node
from pi_gw_panel.subs.parsers import safe_port


def _b64decode(text: str) -> str:
    text = "".join(text.split())
    pad = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + pad).decode("utf-8", "replace")
    except binascii.Error:
        # P3: base64/vless is the catch-all sniff — a mis-sniffed HTML/captcha page isn't
        # valid base64; degrade to zero nodes instead of throwing out of the whole refresh.
        return ""


def parse(body: str, *, limit: int | None = None) -> list[Node]:
    text = body.strip()
    if "vless://" not in text:
        text = _b64decode(text)
    nodes = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("vless://"):
            n = _parse_vless(line)
            if n is not None:
                nodes.append(n)
                if limit is not None and len(nodes) >= limit:
                    break
    return nodes


def _parse_vless(uri: str) -> Node | None:
    try:
        parts = urllib.parse.urlsplit(uri)
        addr = parts.hostname or ""
        port_n = parts.port
    except ValueError:
        return None
    if parts.scheme != "vless" or not parts.username or not addr or port_n is None:
        return None
    q = urllib.parse.parse_qs(parts.query)

    def g(k, d=""):
        return q.get(k, [d])[0]

    network = "xhttp" if g("type") in ("xhttp", "splithttp") else "tcp"
    transport = "xhttp" if network == "xhttp" else "vision"
    # explicit `security` wins; else reality iff a reality public key is present, else tls
    security = g("security") or ("reality" if g("pbk") else "tls")
    port_n = safe_port(port_n)
    if port_n is None:
        return None
    return Node(
        id=None, name=urllib.parse.unquote(parts.fragment) or addr, address=addr, port=port_n,
        uuid=urllib.parse.unquote(parts.username),
        transport=transport, network=network, security=security,
        sni=g("sni"), public_key=g("pbk"), short_id=g("sid"),
        fingerprint=g("fp", "chrome"), flow=g("flow", "xtls-rprx-vision"),
        path=g("path"), host=g("host"), mode=g("mode"), alpn=g("alpn"),
    )
