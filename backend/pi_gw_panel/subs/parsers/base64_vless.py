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
    addr, _, port = hostport.partition(":")
    q = urllib.parse.parse_qs(query)

    def g(k, d=""):
        return q.get(k, [d])[0]

    network = "xhttp" if g("type") in ("xhttp", "splithttp") else "tcp"
    transport = "xhttp" if network == "xhttp" else "vision"
    # explicit `security` wins; else reality iff a reality public key is present, else tls
    security = g("security") or ("reality" if g("pbk") else "tls")
    port_n = safe_port(port)
    if port_n is None:
        return None
    return Node(
        id=None, name=frag or addr, address=addr, port=port_n, uuid=userinfo,
        transport=transport, network=network, security=security,
        sni=g("sni"), public_key=g("pbk"), short_id=g("sid"),
        fingerprint=g("fp", "chrome"), flow=g("flow", "xtls-rprx-vision"),
        path=g("path"), host=g("host"), mode=g("mode"), alpn=g("alpn"),
    )
