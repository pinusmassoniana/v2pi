import yaml
from pi_gw_panel.models import Node


def parse(body: str) -> list[Node]:
    data = yaml.safe_load(body)
    proxies = data.get("proxies", []) if isinstance(data, dict) else []
    nodes = []
    for p in proxies:
        if p.get("type") != "vless":
            continue
        ro = p.get("reality-opts") or {}
        nodes.append(Node(
            id=None, name=str(p.get("name", p.get("server", ""))),
            address=str(p.get("server", "")), port=int(p.get("port", 443)),
            uuid=str(p.get("uuid", "")),
            transport="xhttp" if p.get("network") in ("xhttp", "splithttp") else "vision",
            sni=str(p.get("servername", p.get("sni", ""))),
            public_key=str(ro.get("public-key", "")), short_id=str(ro.get("short-id", "")),
            fingerprint=str(p.get("client-fingerprint", "chrome")),
            flow=str(p.get("flow", "xtls-rprx-vision")),
        ))
    return nodes
