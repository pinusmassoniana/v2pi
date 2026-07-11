"""Single source of truth for the active node's last health snapshot — real-request
result, egress IP, and freshness (`checked_at`). Shared by the live traffic WS frame and
the Network status panel so they never disagree (audit F3). Returns None when no node is
active or the active node has no recorded health yet."""
from datetime import datetime, timezone
from pi_gw_panel.health import geo

# The active node is real-probed ~every 60s; anything older than this means the monitor loop
# died or we just restarted and are serving a pre-restart snapshot — flag it so the UI can say so
# instead of showing an arbitrarily-old green "real_ok".
_STALE_SEC = 180.0


def _is_stale(checked_at: str | None) -> bool:
    if not checked_at:
        return True
    try:
        t = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() > _STALE_SEC


def active_health(store) -> dict | None:
    aid = store.get_setting("active_node_id")
    if not aid:
        return None
    try:
        h = store.get_health(int(aid))
    except (TypeError, ValueError):
        return None
    if h is None:
        return None
    return {
        "node_id": h.node_id,
        "real_ok": h.last_real_ok,
        "latency_ms": h.last_real_ms,
        "egress_ip": h.egress_ip,
        "egress_ip6": h.egress_ip6,
        "egress_cc": geo.country_code(h.egress_ip),     # country flag next to the egress (v4)
        "egress_cc6": geo.country_code(h.egress_ip6),   # and v6
        "checked_at": h.checked_at,
        "stale": _is_stale(h.checked_at),   # true → snapshot is old (dead monitor / just restarted)
        "lat_history": list(h.lat_history or []),   # recent latencies → dashboard sparkline (B)
    }
