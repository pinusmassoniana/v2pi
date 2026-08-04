"""Single source of truth for the active node's last health snapshot — real-request
result, egress IP, and freshness (`checked_at`). Shared by the live traffic WS frame and
the Network status panel so they never disagree (audit F3). Returns None when no node is
active or the active node has no recorded health yet."""
from pi_gw_panel.health import geo
from pi_gw_panel.health.selection import (
    DEFAULT_FRESHNESS_TTL, active_freshness_ttl, checked_at_age_seconds, health_age_seconds,
    ranked_nodes, standby_freshness_ttl,
)

_UNSET = object()

# The active node is real-probed ~every 60s; anything older than this means the monitor loop
# died or we just restarted and are serving a pre-restart snapshot — flag it so the UI can say so
# instead of showing an arbitrarily-old green "real_ok". Only a fallback for callers with no
# store (see `_is_stale`) — the live path derives the real threshold from `health_active_interval`.
_STALE_SEC = DEFAULT_FRESHNESS_TTL


def _is_stale(checked_at: str | None, ttl: float = _STALE_SEC) -> bool:
    age = checked_at_age_seconds(checked_at)
    return age is None or age > ttl


def health_status(store, *, now: float | None = None,
                  freshness_ttl: float | None = None, standby_ttl=_UNSET) -> dict:
    """Freshness and standby eligibility for truthful cross-layer status reporting.

    `freshness_ttl` judges the ACTIVE node (refreshed every `health_active_interval`);
    standbys are judged by their own, much slower sweep cadence. Using one budget for both
    made the panel report zero eligible standbys — and `failover_ready: false` — for most of
    every sweep cycle, and these counts must agree with what `failover.run` actually does."""
    if freshness_ttl is None:
        freshness_ttl = active_freshness_ttl(store)
    if standby_ttl is _UNSET:
        standby_ttl = standby_freshness_ttl(store)
    active_v = store.get_setting("active_node_id")
    try:
        active_id = int(active_v) if active_v else None
    except (TypeError, ValueError):
        active_id = None
    active = store.get_health(active_id) if active_id is not None else None
    age = health_age_seconds(active, now)
    health = {item.node_id: item for item in store.list_health()}
    eligible = ranked_nodes(
        store.list_nodes(), health, exclude_id=active_id,
        # With the sweep off nothing can mark a standby alive; failover then leans on its
        # pre-promotion preflight, so the count must not demand evidence that cannot exist.
        require_alive=standby_ttl is not None,
        now=now, freshness_ttl=standby_ttl,
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
        # Same TTL derivation `health_status()`/failover use (audit FIX-E-3): a fixed 180s here
        # while those derive from `health_active_interval` made the two contradict each other
        # whenever the interval was configured above 90s.
        "stale": _is_stale(h.checked_at, active_freshness_ttl(store)),
        "lat_history": list(h.lat_history or []),   # recent latencies → dashboard sparkline (B)
    }
