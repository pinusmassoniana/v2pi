import ipaddress
import re
from pi_gw_panel.models import RoutingRule

# Built-in presets — name → (title, [(type, value, action), …]). Imported on demand and
# returned as a *staged* ruleset (the caller reviews + Saves; importing never persists).
PRESETS: dict[str, dict] = {
    "ru-direct": {"title": "RU-direct — keep Russian traffic off the tunnel",
                  "rules": [("geoip", "ru", "direct"), ("geosite", "category-ru", "direct")]},
    "block-ads": {"title": "Block ads & trackers",
                  "rules": [("geosite", "category-ads-all", "block")]},
    "cn-direct": {"title": "CN-direct — Chinese traffic off the tunnel",
                  "rules": [("geoip", "cn", "direct"), ("geosite", "cn", "direct")]},
    "lan-direct": {"title": "LAN-direct — private ranges direct (explicit)",
                   "rules": [("ip", "192.168.0.0/16", "direct"), ("ip", "10.0.0.0/8", "direct")]},
}

# Back-compat alias used by the old preset import path / tests.
RU_DIRECT_PRESET: list[RoutingRule] = [
    RoutingRule(id=None, position=i, type=t, value=v, action=a)
    for i, (t, v, a) in enumerate(PRESETS["ru-direct"]["rules"])
]

_TYPES = {"geoip", "geosite", "domain", "ip", "port"}
_ACTIONS = {"direct", "proxy", "block"}


def preset_rules(name: str) -> list[RoutingRule] | None:
    spec = PRESETS.get(name)
    if spec is None:
        return None
    return [RoutingRule(id=None, position=i, type=t, value=v, action=a)
            for i, (t, v, a) in enumerate(spec["rules"])]


def _split(value: str) -> list[str]:
    """Split a rule value on commas/newlines into trimmed, non-empty tokens (multi-value)."""
    return [v.strip() for v in re.split(r"[,\n]", value or "") if v.strip()]


def validate_rule(r: RoutingRule) -> str | None:
    """Return an error message if the rule is invalid, else None."""
    if r.type not in _TYPES:
        return f"unknown type {r.type!r}"
    if r.action not in _ACTIONS:
        return f"unknown action {r.action!r}"
    vals = _split(r.value)
    if not vals:
        return "empty value"
    if r.type == "port":
        for v in vals:
            if not re.fullmatch(r"\d{1,5}(-\d{1,5})?", v):
                return f"bad port {v!r}"
    elif r.type == "ip":
        for v in vals:
            try:
                ipaddress.ip_network(v, strict=False)
            except ValueError:
                return f"bad ip/cidr {v!r}"
    return None


def validate_routing(rules: list[RoutingRule], default_action: str) -> tuple[bool, str]:
    """Structural validation of a ruleset (no xray needed). Returns (ok, error)."""
    if default_action not in _ACTIONS:
        return False, f"unknown default action {default_action!r}"
    for i, r in enumerate(rules):
        err = validate_rule(r)
        if err:
            return False, f"rule {i + 1}: {err}"
    return True, ""


def _values_field(r: RoutingRule) -> list[str]:
    vals = _split(r.value)
    if r.type == "geoip":
        return ["geoip:" + v for v in vals]
    if r.type == "geosite":
        return ["geosite:" + v for v in vals]
    return vals   # ip / domain literals


def _rule_to_field(r: RoutingRule) -> dict:
    """Translate one stored RoutingRule into an xray `routing.rules` field rule.
    `geoip`/`geosite` get their xray prefixes; `domain`/`ip` are literals (multi-value →
    a list); `port` stays a string ("443" / "1000-2000" / "80,443")."""
    field = {"type": "field", "outboundTag": r.action}
    if r.type in ("geoip", "ip"):
        field["ip"] = _values_field(r)
    elif r.type in ("geosite", "domain"):
        field["domain"] = _values_field(r)
    elif r.type == "port":
        field["port"] = (r.value or "").strip()
    else:
        raise ValueError(f"unknown routing rule type: {r.type!r}")
    return field


def rules_to_xray(rules: list[RoutingRule], default_action: str) -> list[dict]:
    """Build the full xray `routing.rules` list: Wave-0's private→direct first, then each
    enabled rule in `position` order, then a `default_action` catch-all for tcp,udp.

    With `rules=[]` and `default_action="proxy"` this is exactly the Wave-0 routing."""
    out: list[dict] = [{"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"}]
    for r in sorted(rules, key=lambda x: x.position):
        if getattr(r, "enabled", True):
            out.append(_rule_to_field(r))
    out.append({"type": "field", "network": "tcp,udp", "outboundTag": default_action})
    return out
