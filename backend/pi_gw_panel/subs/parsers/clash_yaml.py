import yaml
from pi_gw_panel.models import SS_METHODS, Node, ss_password_issue
from pi_gw_panel.subs.parsers import note_skip, safe_port


# Real clash configs nest a handful of levels (proxies → proxy → opts → headers → value);
# anything past this is a bomb, not a config.
MAX_YAML_DEPTH = 50


class _NoAliasLoader(yaml.SafeLoader):
    """SafeLoader that refuses YAML aliases (P2) and caps nesting depth. safe_load blocks
    arbitrary object construction but NOT anchor/alias amplification ("billion laughs"): a
    small (<5MB) doc can expand to gigabytes and OOM. Anchors (&a) are harmless alone; refusing
    to expand the alias (*a) — where the blow-up actually happens — stops it without a
    threshold guess. compose_node also recurses once per nesting level, so a deeply nested
    document (`[[[[…]]]]`) blows the interpreter stack before any of our own code runs; the
    depth cap turns that into an ordinary rejected feed."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._depth = 0

    def compose_node(self, parent, index):
        if self.check_event(yaml.events.AliasEvent):
            raise yaml.YAMLError("YAML aliases are disabled (anchor/alias amplification guard)")
        if self._depth >= MAX_YAML_DEPTH:
            raise yaml.YAMLError(f"YAML nesting deeper than {MAX_YAML_DEPTH} levels is rejected")
        self._depth += 1
        try:
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1


def _opts_path_host(p: dict) -> tuple[str, str]:
    """Best-effort path + Host from clash transport opts (xhttp/ws/h2/http variants).
    Clash has no single canonical place for these, so probe the common keys."""
    for key in ("xhttp-opts", "ws-opts", "h2-opts", "http-opts"):
        o = p.get(key)
        if not isinstance(o, dict):
            continue
        path = o.get("path")
        if isinstance(path, list):
            path = path[0] if path else ""
        headers = o.get("headers") if isinstance(o.get("headers"), dict) else {}
        host = headers.get("Host") or headers.get("host") or o.get("host") or ""
        if isinstance(host, list):
            host = host[0] if host else ""
        if path or host:
            return str(path or ""), str(host or "")
    return "", ""


def parse(body: str, *, limit: int | None = None, skipped: dict | None = None) -> list[Node]:
    data = yaml.load(body, Loader=_NoAliasLoader)   # SafeLoader + no-alias amplification guard
    proxies = data.get("proxies", []) if isinstance(data, dict) else []
    nodes = []
    for p in proxies:
        if not isinstance(p, dict):
            note_skip(skipped, "invalid")
            continue
        kind = str(p.get("type") or "")
        if kind not in ("vless", "trojan", "ss"):
            note_skip(skipped, p.get("type"))
            continue
        if kind != "vless":
            node = _trojan(p) if kind == "trojan" else _shadowsocks(p)
            if node is None:
                note_skip(skipped, "invalid")
                continue
            nodes.append(node)
            if limit is not None and len(nodes) >= limit:
                break
            continue
        ro = p.get("reality-opts")
        ro = ro if isinstance(ro, dict) else {}
        is_xhttp = p.get("network") in ("xhttp", "splithttp")
        pbk = str(ro.get("public-key", ""))
        path, host = _opts_path_host(p)
        alpn = p.get("alpn")
        alpn = ",".join(str(a) for a in alpn) if isinstance(alpn, list) else str(alpn or "")
        port_n = safe_port(p.get("port"))
        if port_n is None:
            note_skip(skipped, "invalid")
            continue
        nodes.append(Node(
            id=None, name=str(p.get("name", p.get("server", ""))),
            address=str(p.get("server", "")), port=port_n,
            uuid=str(p.get("uuid", "")),
            transport="xhttp" if is_xhttp else "vision",
            network="xhttp" if is_xhttp else "tcp",
            security="reality" if pbk else "tls",   # normalize() also enforces this
            sni=str(p.get("servername", p.get("sni", ""))),
            public_key=pbk, short_id=str(ro.get("short-id", "")),
            fingerprint=str(p.get("client-fingerprint", "chrome")),
            flow=str(p.get("flow", "xtls-rprx-vision")),
            path=path, host=host, alpn=alpn,
        ))
        if limit is not None and len(nodes) >= limit:
            break
    return nodes


def _trojan(p: dict) -> Node | None:
    """A clash `trojan` proxy. Only the plain-TCP form: a ws/grpc one is refused rather than
    imported as TCP, because the panel builds no other transport for it."""
    port = safe_port(p.get("port"))
    address = str(p.get("server", ""))
    password = str(p.get("password", ""))
    if port is None or not address or not password:
        return None
    if str(p.get("network") or "tcp").lower() not in ("tcp", "original", "none"):
        return None
    ro = p.get("reality-opts")
    ro = ro if isinstance(ro, dict) else {}
    pbk = str(ro.get("public-key", ""))
    alpn = p.get("alpn")
    return Node(
        id=None, name=str(p.get("name", address)), address=address, port=port,
        uuid="", protocol="trojan", password=password,
        security="reality" if pbk else "tls",
        sni=str(p.get("sni", p.get("servername", ""))), public_key=pbk,
        short_id=str(ro.get("short-id", "")),
        fingerprint=str(p.get("client-fingerprint", "chrome")),
        alpn=",".join(str(a) for a in alpn) if isinstance(alpn, list) else str(alpn or ""),
    )


def _shadowsocks(p: dict) -> Node | None:
    """A clash `ss` proxy. A `plugin` (obfs, v2ray-plugin) changes what goes on the wire, and a
    cipher xray cannot build is no better than none: both are refused rather than imported."""
    port = safe_port(p.get("port"))
    address = str(p.get("server", ""))
    password = str(p.get("password", ""))
    method = str(p.get("cipher", "")).strip().lower()
    if port is None or not address or not password or method not in SS_METHODS or p.get("plugin"):
        return None
    if ss_password_issue(method, password):
        return None
    return Node(id=None, name=str(p.get("name", address)), address=address, port=port,
                uuid="", protocol="shadowsocks", password=password, method=method)
