from pi_gw_panel.models import RoutingRule

# Built-in "keep Russian traffic off the tunnel" preset, imported on demand.
RU_DIRECT_PRESET: list[RoutingRule] = [
    RoutingRule(id=None, position=0, type="geoip", value="ru", action="direct"),
    RoutingRule(id=None, position=1, type="geosite", value="category-ru", action="direct"),
]


def _rule_to_field(r: RoutingRule) -> dict:
    """Translate one stored RoutingRule into an xray `routing.rules` field rule.
    `geoip`/`geosite` get their xray prefixes; `domain`/`ip`/`port` are literals.
    `action` (direct|proxy|block) maps straight to the matching outbound tag."""
    field = {"type": "field", "outboundTag": r.action}
    if r.type == "geoip":
        field["ip"] = ["geoip:" + r.value]
    elif r.type == "ip":
        field["ip"] = [r.value]
    elif r.type == "geosite":
        field["domain"] = ["geosite:" + r.value]
    elif r.type == "domain":
        field["domain"] = [r.value]
    elif r.type == "port":
        field["port"] = r.value
    else:
        raise ValueError(f"unknown routing rule type: {r.type!r}")
    return field


def rules_to_xray(rules: list[RoutingRule], default_action: str) -> list[dict]:
    """Build the full xray `routing.rules` list: Wave-0's private→direct first, then
    each rule in `position` order, then a `default_action` catch-all for tcp,udp.

    With `rules=[]` and `default_action="proxy"` this is exactly the Wave-0 routing."""
    out: list[dict] = [{"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"}]
    for r in sorted(rules, key=lambda x: x.position):
        out.append(_rule_to_field(r))
    out.append({"type": "field", "network": "tcp,udp", "outboundTag": default_action})
    return out
