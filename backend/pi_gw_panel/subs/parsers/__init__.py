def safe_port(value, default: int = 443) -> int | None:
    """Defensive node-port parse shared by the subscription parsers (audit B4): None for
    garbage or out-of-range values, so the caller skips that node instead of crashing the
    refresh or shipping an invalid port into the xray config."""
    try:
        port = int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


# Per-field byte/char caps for untrusted feed strings (audit P2). Under the 5MB body cap a
# feed could pack a few nodes with megabyte-long fields, bloating the DB and every config
# render; clamp them at the single choke point (reconcile) before they reach the store.
_STR_LIMITS = {"name": 256, "address": 253, "sni": 253, "host": 253, "uuid": 128}
_DEFAULT_STR_LIMIT = 512
_CLAMP_FIELDS = ("name", "address", "uuid", "sni", "path", "host", "alpn",
                 "public_key", "short_id", "fingerprint", "flow", "mode", "note")


def clamp_node_fields(node):
    """Truncate a node's untrusted string fields to sane bounds (name<=256, address/sni/host
    <=253, uuid<=128, everything else<=512). Mutates in place and returns the node."""
    for attr in _CLAMP_FIELDS:
        v = getattr(node, attr, "")
        if isinstance(v, str):
            limit = _STR_LIMITS.get(attr, _DEFAULT_STR_LIMIT)
            if len(v) > limit:
                setattr(node, attr, v[:limit])
    return node
