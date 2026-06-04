import json
from pi_gw_panel.models import Node
from pi_gw_panel.subs.parsers import base64_vless, clash_yaml, json_nodes


def detect(body: str) -> str:
    """Sniff the subscription format → 'json' | 'clash' | 'base64/vless'. Same order as
    parse_subscription, exposed so the dry-run preview can report what it saw."""
    text = body.strip()
    if text[:1] in ("[", "{"):
        try:
            json.loads(text)
            return "json"
        except ValueError:
            pass
    if "proxies:" in text:
        return "clash"
    return "base64/vless"


def parse_subscription(body: str) -> list[Node]:
    """Sniff the subscription format and delegate. Order: JSON, clash-yaml, base64/vless."""
    fmt = detect(body)
    text = body.strip()
    if fmt == "json":
        return json_nodes.parse(text)
    if fmt == "clash":
        return clash_yaml.parse(text)
    return base64_vless.parse(text)
