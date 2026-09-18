import base64
import binascii
import re
import urllib.parse
from pi_gw_panel.models import SS_METHODS, Node, ss_password_issue
from pi_gw_panel.subs.parsers import note_skip, safe_port

# A line that is some OTHER protocol's share link (trojan://…, hysteria2://…), as opposed to
# free text: the scheme is what gets counted.
_OTHER_SCHEME = re.compile(r"^([a-z][a-z0-9+.\-]{1,15})://")


def _b64decode(text: str) -> str:
    text = "".join(text.split())
    pad = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + pad).decode("utf-8", "replace")
    except binascii.Error:
        # P3: base64/vless is the catch-all sniff — a mis-sniffed HTML/captcha page isn't
        # valid base64; degrade to zero nodes instead of throwing out of the whole refresh.
        return ""


def parse(body: str, *, limit: int | None = None, skipped: dict | None = None) -> list[Node]:
    text = body.strip()
    if not any(f"{scheme}://" in text for scheme in _PARSERS):
        text = _b64decode(text)
    nodes = []
    for line in text.splitlines():
        line = line.strip()
        # Only URI-shaped lines are looked at. A mis-sniffed HTML page or a base64 body that
        # decoded to noise would otherwise report thousands of "other" entries.
        other = _OTHER_SCHEME.match(line)
        if not other:
            continue
        parser = _PARSERS.get(other.group(1))
        if parser is None:
            note_skip(skipped, other.group(1))
            continue
        node = parser(line)
        if node is None:
            note_skip(skipped, "invalid")
            continue
        nodes.append(node)
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
    # An explicit `security` wins only if it is one we support: the feed is untrusted, and
    # `security=none` would otherwise land in streamSettings verbatim and carry VLESS in the
    # clear. Anything else falls back to reality iff a reality public key is present, else tls.
    # (Node.normalize re-checks this — this keeps the parsed node honest too.)
    requested = g("security").strip().lower()
    security = requested if requested in ("reality", "tls") else ("reality" if g("pbk") else "tls")
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


def _parse_trojan(uri: str) -> Node | None:
    """`trojan://password@host:port?security=…&sni=…#name`.

    Only the TCP form is taken. A `type=ws`/`grpc` entry is refused rather than imported as
    plain TCP: the panel builds no other transport for it, so it would be a node that looks
    fine in the list and can never connect.
    """
    try:
        parts = urllib.parse.urlsplit(uri)
        addr = parts.hostname or ""
        port_n = parts.port
    except ValueError:
        return None
    if parts.scheme != "trojan" or not parts.username or not addr or port_n is None:
        return None
    q = urllib.parse.parse_qs(parts.query)

    def g(k, d=""):
        return q.get(k, [d])[0]

    if g("type", "tcp").strip().lower() not in ("", "tcp", "original", "none"):
        return None
    port_n = safe_port(port_n)
    if port_n is None:
        return None
    requested = g("security").strip().lower()
    security = requested if requested in ("reality", "tls") else ("reality" if g("pbk") else "tls")
    return Node(
        id=None, name=urllib.parse.unquote(parts.fragment) or addr, address=addr, port=port_n,
        uuid="", protocol="trojan", password=urllib.parse.unquote(parts.username),
        security=security, sni=g("sni") or g("peer"), public_key=g("pbk"), short_id=g("sid"),
        fingerprint=g("fp", "chrome"), alpn=g("alpn"),
    )


def _parse_ss(uri: str) -> Node | None:
    """`ss://` in both forms a feed uses: SIP002 (`ss://b64(method:password)@host:port#name`)
    and the legacy whole-URI base64 (`ss://b64(method:password@host:port)#name`).

    An entry carrying a `plugin=` (obfs, v2ray-plugin, shadow-tls) is refused for the same
    reason a ws Trojan is: it changes what goes on the wire, and the panel builds a plain
    Shadowsocks outbound.
    """
    body = uri[len("ss://"):]
    body, _, fragment = body.partition("#")
    body, _, query = body.partition("?")
    if "plugin=" in query:
        return None
    if "@" not in body:
        body = _b64decode(body)          # legacy: the whole thing is one base64 blob
    userinfo, sep, hostport = body.rpartition("@")
    if not sep or not userinfo:
        return None
    userinfo = urllib.parse.unquote(userinfo)
    if ":" not in userinfo:
        userinfo = _b64decode(userinfo)  # SIP002: websafe-base64("method:password")
    method, sep, password = userinfo.partition(":")
    if not sep or not password:
        return None
    method = method.strip().lower()
    if method not in SS_METHODS:         # including the ciphers xray itself refuses to build
        return None
    if ss_password_issue(method, password):   # a 2022 key xray would refuse to start on
        return None
    try:
        parts = urllib.parse.urlsplit(f"//{hostport}")
        addr = parts.hostname or ""
        port_n = parts.port
    except ValueError:
        return None
    if not addr or port_n is None:
        return None
    port_n = safe_port(port_n)
    if port_n is None:
        return None
    return Node(
        id=None, name=urllib.parse.unquote(fragment) or addr, address=addr, port=port_n,
        uuid="", protocol="shadowsocks", password=password, method=method,
    )


# Scheme → parser. A line whose scheme is absent is counted as skipped, by that scheme's name.
_PARSERS = {"vless": _parse_vless, "trojan": _parse_trojan, "ss": _parse_ss}
