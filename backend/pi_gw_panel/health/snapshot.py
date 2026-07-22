"""Single source of truth for the active node's last health snapshot — real-request
result, egress IP, and freshness (`checked_at`). Shared by the live traffic WS frame and
the Network status panel so they never disagree (audit F3). Returns None when no node is
active or the active node has no recorded health yet."""
from pi_gw_panel.health import geo
from pi_gw_panel.health.selection import (
    DEFAULT_FRESHNESS_TTL, checked_at_age_seconds, health_age_seconds, ranked_nodes,
)

# The active node is real-probed ~every 60s; anything older than this means the monitor loop
# died or we just restarted and are serving a pre-restart snapshot — flag it so the UI can say so
# instead of showing an arbitrarily-old green "real_ok".
_STALE_SEC = DEFAULT_FRESHNESS_TTL


def _is_stale(checked_at: str | None) -> bool:
    age = checked_at_age_seconds(checked_at)
    return age is None or age > _STALE_SEC


def health_status(store, *, now: float | None = None,
                  freshness_ttl: float = _STALE_SEC) -> dict:
    """Freshness and standby eligibility for truthful cross-layer status reporting."""
    active_v = store.get_setting("active_node_id")
    try:
        active_id = int(active_v) if active_v else None
    except (TypeError, ValueError):
        active_id = None
    active = store.get_health(active_id) if active_id is not None else None
    age = health_age_seconds(active, now)
    health = {item.node_id: item for item in store.list_health()}
    eligible = ranked_nodes(
        store.list_nodes(), health, exclude_id=active_id, require_alive=True,
        now=now, freshness_ttl=freshness_ttl,
    ) if active_id is not None else []
    return {
        "active_health_fresh": age is not None and age <= freshness_ttl,
        "active_health_age_sec": age,
        "health_freshness_ttl_sec": freshness_ttl,
        "eligible_standby_count": len(eligible),
    }


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
