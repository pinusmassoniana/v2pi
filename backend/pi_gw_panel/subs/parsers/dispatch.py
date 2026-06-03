import json
from pi_gw_panel.models import Node
from pi_gw_panel.subs.parsers import base64_vless, clash_yaml, json_nodes


def parse_subscription(body: str) -> list[Node]:
    """Sniff the subscription format and delegate. Order: JSON, clash-yaml, base64/vless."""
    text = body.strip()
    if text[:1] in ("[", "{"):
        try:
            json.loads(text)
            return json_nodes.parse(text)
        except ValueError:
            pass
    if "proxies:" in text:
        return clash_yaml.parse(text)
    return base64_vless.parse(text)
