import json
from pi_gw_panel.models import Node


def parse(body: str) -> list[Node]:
    data = json.loads(body)
    items = data if isinstance(data, list) else data.get("nodes", [])
    nodes = []
    for it in items:
        nodes.append(Node(
            id=None, name=str(it.get("name", it.get("address", ""))),
            address=str(it["address"]), port=int(it.get("port", 443)),
            uuid=str(it.get("uuid", "")), transport=it.get("transport", "vision"),
            sni=str(it.get("sni", "")), public_key=str(it.get("public_key", "")),
            short_id=str(it.get("short_id", "")),
            fingerprint=str(it.get("fingerprint", "chrome")),
            flow=str(it.get("flow", "xtls-rprx-vision")),
        ))
    return nodes
