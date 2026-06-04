import json
from pi_gw_panel.models import Node


def parse(body: str) -> list[Node]:
    data = json.loads(body)
    items = data if isinstance(data, list) else data.get("nodes", [])
    nodes = []
    for it in items:
        net = str(it.get("network", ""))
        transport = it.get("transport") or ("xhttp" if net in ("xhttp", "splithttp") else "vision")
        pbk = str(it.get("public_key", ""))
        nodes.append(Node(
            id=None, name=str(it.get("name", it.get("address", ""))),
            address=str(it["address"]), port=int(it.get("port", 443)),
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
    return nodes
