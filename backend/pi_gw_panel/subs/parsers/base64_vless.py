import base64
import urllib.parse
from pi_gw_panel.models import Node


def _b64decode(text: str) -> str:
    text = "".join(text.split())
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad).decode("utf-8", "replace")


def parse(body: str) -> list[Node]:
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
    return nodes


def _parse_vless(uri: str) -> Node | None:
    rest = uri[len("vless://"):]
    frag = ""
    if "#" in rest:
        rest, frag = rest.split("#", 1)
        frag = urllib.parse.unquote(frag)
    userinfo, sep, hostport_q = rest.partition("@")
    if not sep:
        return None
    hostport, _, query = hostport_q.partition("?")
    host, _, port = hostport.partition(":")
    q = urllib.parse.parse_qs(query)

    def g(k, d=""):
        return q.get(k, [d])[0]

    transport = "xhttp" if g("type") in ("xhttp", "splithttp") else "vision"
    return Node(
        id=None, name=frag or host, address=host, port=int(port or 443), uuid=userinfo,
        transport=transport, sni=g("sni"), public_key=g("pbk"),
        short_id=g("sid"), fingerprint=g("fp", "chrome"), flow=g("flow", "xtls-rprx-vision"),
    )
