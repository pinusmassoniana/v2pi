import yaml
from pi_gw_panel.models import Node
from pi_gw_panel.subs.parsers import safe_port


class _NoAliasLoader(yaml.SafeLoader):
    """SafeLoader that refuses YAML aliases (P2). safe_load blocks arbitrary object
    construction but NOT anchor/alias amplification ("billion laughs"): a small (<5MB) doc
    can expand to gigabytes and OOM. Anchors (&a) are harmless alone; refusing to expand the
    alias (*a) — where the blow-up actually happens — stops it without a threshold guess."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.events.AliasEvent):
            raise yaml.YAMLError("YAML aliases are disabled (anchor/alias amplification guard)")
        return super().compose_node(parent, index)


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


def parse(body: str) -> list[Node]:
    data = yaml.load(body, Loader=_NoAliasLoader)   # SafeLoader + no-alias amplification guard
    proxies = data.get("proxies", []) if isinstance(data, dict) else []
    nodes = []
    for p in proxies:
        if p.get("type") != "vless":
            continue
        ro = p.get("reality-opts") or {}
        is_xhttp = p.get("network") in ("xhttp", "splithttp")
        pbk = str(ro.get("public-key", ""))
        path, host = _opts_path_host(p)
        alpn = p.get("alpn")
        alpn = ",".join(str(a) for a in alpn) if isinstance(alpn, list) else str(alpn or "")
        port_n = safe_port(p.get("port"))
        if port_n is None:
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
    return nodes
