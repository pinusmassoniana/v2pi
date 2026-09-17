import ipaddress
import re
from pi_gw_panel.geo_data import DATASETS, EXT_FILES
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
    # A3: the inverse of ru-direct, and the one most people actually want — everything goes
    # direct, and only what is blocked in Russia takes the tunnel. It needs the `ru` dataset
    # installed (System › Panel › Geo data); the import warns when it is not.
    "ru-blocked-only": {"title": "Only blocked-in-RU through the tunnel (needs the RU geo data)",
                        "default_action": "direct",
                        "dataset": "ru",
                        "rules": [("geosite", "ru-blocked", "proxy"),
                                  ("geoip", "ru-blocked", "proxy")]},
}

# Categories big enough that loading them measurably slows every apply and every xray start
# (~1.3 s for ru-blocked-all against ~70 ms for ru-blocked, measured on the pinned xray).
SLOW_CATEGORIES = {("ru", "ru-blocked-all"), ("ru", "refilter")}

# `device` matches the SOURCE address — one pinned client — so it is an override: it decides
# where that device's traffic goes regardless of what the destination rules below would say.
_TYPES = {"geoip", "geosite", "domain", "ip", "port", "device"}
_ACTIONS = {"direct", "proxy", "block"}


def preset_rules(name: str) -> list[RoutingRule] | None:
    spec = PRESETS.get(name)
    if spec is None:
        return None
    dataset = spec.get("dataset", "")
    return [RoutingRule(id=None, position=i, type=t, value=v, action=a,
                        dataset=dataset if t in ("geoip", "geosite") else "")
            for i, (t, v, a) in enumerate(spec["rules"])]


def _split(value: str) -> list[str]:
    """Split a rule value on commas/newlines into trimmed, non-empty tokens (multi-value)."""
    return [v.strip() for v in re.split(r"[,\n]", value or "") if v.strip()]


def _is_private_net(value: str) -> bool:
    """True when the whole literal sits inside a range xray's `geoip:private` covers."""
    try:
        net = ipaddress.ip_network(value, strict=False)
    except ValueError:
        return False
    return net.is_private or net.is_loopback or net.is_link_local


def _shadowed_by_private(r: RoutingRule, vals: list[str]) -> str | None:
    """`rules_to_xray` puts the built-in `geoip:private → direct` ahead of every user rule, and
    xray takes the FIRST match. A user rule that targets a private range with any action other
    than `direct` can therefore never fire — it would validate, display as active, and silently
    do nothing (a `block` on a private range would quietly go direct instead). Reject it here so
    the operator is told, rather than trusting a rule the router cannot reach."""
    if r.action == "direct":
        return None
    hint = ("the built-in 'geoip:private → direct' rule is matched first, so this rule could "
            f"never send it to {r.action!r} — only 'direct' is reachable for private ranges")
    if r.type == "ip":
        for v in vals:
            if _is_private_net(v):
                return f"{v!r} is a private range and {hint}"
    elif r.type == "geoip":
        if any(v.lower() == "private" for v in vals):
            return f"'geoip:private' and {hint}"
    return None


def validate_rule(r: RoutingRule) -> str | None:
    """Return an error message if the rule is invalid, else None."""
    if r.type not in _TYPES:
        return f"unknown type {r.type!r}"
    dataset = getattr(r, "dataset", "") or ""
    if dataset:
        if dataset not in DATASETS or dataset == "stock":
            return f"unknown geo dataset {dataset!r}"
        if r.type not in ("geoip", "geosite"):
            return f"a {r.type} rule carries literals, so it has no geo dataset"
    if r.action not in _ACTIONS:
        return f"unknown action {r.action!r}"
    vals = _split(r.value)
    if not vals:
        return "empty value"
    if r.type == "device":
        for v in vals:
            try:
                address = ipaddress.ip_address(v)
            except ValueError:
                return f"{v!r} is not an IPv4 address"
            if address.version != 4:
                # A client's IPv6 address is a SLAAC/privacy address that changes by itself, so a
                # v6 source rule would match until the device rotated it and then quietly stop.
                return "a device rule matches the client's IPv4 address only"
    if r.type == "port":
        for v in vals:
            if not re.fullmatch(r"\d{1,5}(-\d{1,5})?", v):
                return f"bad port {v!r}"
            bounds = [int(part) for part in v.split("-")]
            if not all(1 <= port <= 65535 for port in bounds) or bounds[0] > bounds[-1]:
                return f"bad port {v!r}"
    elif r.type == "ip":
        for v in vals:
            try:
                ipaddress.ip_network(v, strict=False)
            except ValueError:
                return f"bad ip/cidr {v!r}"
    return _shadowed_by_private(r, vals)


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
    dataset = getattr(r, "dataset", "") or ""
    if r.type in ("geoip", "geosite"):
        if dataset:
            # xray reads a non-default data file only through `ext:<file>:<code>`, resolved in
            # the asset dir (geo_data.install_env). Verified against the pinned xray for both
            # the domain and the ip side.
            file = EXT_FILES[dataset][r.type]
            return [f"ext:{file}:{v}" for v in vals]
        return [f"{r.type}:{v}" for v in vals]
    return vals   # ip / domain literals


def _rule_to_field(r: RoutingRule) -> dict:
    """Translate one stored RoutingRule into an xray `routing.rules` field rule.
    `geoip`/`geosite` get their xray prefixes; `domain`/`ip` are literals (multi-value →
    a list); `port` stays a string ("443" / "1000-2000" / "80,443")."""
    field = {"type": "field", "outboundTag": r.action}
    if r.type == "device":
        field["source"] = _split(r.value)
    elif r.type in ("geoip", "ip"):
        field["ip"] = _values_field(r)
    elif r.type in ("geosite", "domain"):
        field["domain"] = _values_field(r)
    elif r.type == "port":
        # emit the same normalized tokens validation checked: a value like "80\n443" passes
        # validate_rule (per-token) but must be emitted as "80,443", not with an embedded newline
        # (which xray rejects), so a "valid" ruleset can't produce a config xray refuses.
        field["port"] = ",".join(_split(r.value))
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
