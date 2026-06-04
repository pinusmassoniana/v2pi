import re
from pi_gw_panel.models import Node, TuningProfile

# Field-value presets the user can stage into the editor (non-persisting). "RU-hardened" is the
# recommended June-2026 anti-DPI stack: fragment + tlshello + UDP noise + QUIC dropped.
PROFILE_PRESETS: dict[str, dict] = {
    "ru-hardened": {
        "title": "RU-hardened — fragment + noise + QUIC drop",
        "fields": {"fingerprint": "chrome", "frag_enabled": True, "frag_packets": "tlshello",
                   "frag_length": "100-200", "frag_interval": "10-20", "quic": "drop",
                   "noise_enabled": True,
                   "noises": [{"type": "rand", "packet": "50-150", "delay": "10-16"}]},
    },
    "stealth-latency": {
        "title": "Stealth (min latency) — fingerprint only, QUIC allowed",
        "fields": {"fingerprint": "chrome", "frag_enabled": False, "quic": "allow"},
    },
    "cdn-xhttp": {
        "title": "CDN / XHTTP — padding + xmux",
        "fields": {"fingerprint": "chrome", "quic": "drop", "xhttp_padding": "100-1000",
                   "xmux_max_concurrency": "16", "xmux_max_connections": "0"},
    },
}

_FP = {"chrome", "firefox", "safari", "ios", "android", "edge", "random",
       "randomized", "randomizednoalpn", ""}
_RANGE = re.compile(r"\d+(-\d+)?$")


def validate_profile(p: TuningProfile) -> tuple[bool, str]:
    """Structural validation of a tuning profile (no xray needed). Returns (ok, error)."""
    if p.quic not in ("allow", "drop", "proxy"):
        return False, f"bad QUIC mode {p.quic!r}"
    if p.fingerprint not in _FP:
        return False, f"unknown fingerprint {p.fingerprint!r}"
    if p.frag_enabled:
        if not (p.frag_packets == "tlshello" or re.fullmatch(r"\d+-\d+", p.frag_packets or "")):
            return False, f"bad fragment packets {p.frag_packets!r} (use 'tlshello' or 'a-b')"
        for fld, val in (("length", p.frag_length), ("interval", p.frag_interval)):
            if not _RANGE.fullmatch(val or ""):
                return False, f"bad fragment {fld} {val!r}"
    if p.doh_enabled and p.doh_url and not p.doh_url.startswith(("http://", "https://")):
        return False, "DoH URL must start with http:// or https://"
    for n in (p.noises or []):
        if not isinstance(n, dict) or n.get("type") not in ("rand", "str", "base64", "hex"):
            return False, f"bad noise entry {n!r}"
    return True, ""


def resolve_profile(store, node: Node) -> TuningProfile | None:
    """Resolve the tuning profile that governs `node`: its explicitly assigned
    profile, else the global default, else None.

    `None` (no store, or no default seeded) is the Wave-0 path — `build_config`
    with `profile=None` stays byte-identical to Wave-0."""
    if store is None:
        return None
    if node.tuning_profile_id is not None:
        p = store.get_profile(node.tuning_profile_id)
        if p is not None:
            return p
        # dangling reference (profile deleted) → fall through to the default
    return store.get_default_profile()
