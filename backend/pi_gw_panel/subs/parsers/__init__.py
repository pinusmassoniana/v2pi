# Aliases a feed may use for the same protocol, so the count reads as one line, not three.
_SKIP_ALIASES = {
    "shadowsocks": "ss", "ssr": "ss", "ss2022": "ss",
    "hysteria": "hysteria2", "hy2": "hysteria2", "hy": "hysteria2",
    "socks5": "socks", "https": "http",
}
# Every other label becomes "other". The keys are echoed back through the API and rendered in
# the UI, so a feed must not be able to choose them: an entry typed `<script>` is "other".
SKIP_LABELS = frozenset({
    "vmess", "trojan", "ss", "hysteria2", "tuic", "wireguard", "socks", "http",
    "snell", "anytls", "juicity", "mieru",
    "invalid",   # a supported entry the parser could not use (bad port, missing address)
    "other",     # a protocol we have no name for
})


def skip_label(raw) -> str:
    """Map an untrusted entry type / URI scheme onto one of SKIP_LABELS."""
    key = str(raw or "").strip().lower()[:32]
    key = _SKIP_ALIASES.get(key, key)
    return key if key in SKIP_LABELS else "other"


def note_skip(skipped: dict | None, raw) -> None:
    """Count one entry a parser dropped. `skipped` is None for callers that don't ask (the
    parsers keep working exactly as before), so no call site is forced to care."""
    if skipped is None:
        return
    label = skip_label(raw)
    skipped[label] = skipped.get(label, 0) + 1


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
# `security` and `transport` are persisted feed strings too (and echoed back out of the API),
# so they are clamped like the rest — `flow` is already here, and `network` is forced to
# tcp/xhttp by Node.normalize().
_CLAMP_FIELDS = ("name", "address", "uuid", "sni", "path", "host", "alpn",
                 "public_key", "short_id", "fingerprint", "flow", "mode", "note",
                 "security", "transport", "password", "method")


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
