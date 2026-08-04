"""Shared "pick the healthiest node" scoring, used by both auto-failover and the
manual connect-best route so the two never disagree (audit NC3)."""
from datetime import datetime, timezone

# The ACTIVE node is real-probed every `health_active_interval` (60 s by default), so 180 s
# means "we missed a couple of checks".
DEFAULT_FRESHNESS_TTL = 180.0
DEFAULT_ACTIVE_INTERVAL = 60.0
DEFAULT_SWEEP_INTERVAL = 1800.0
# Tolerate one fully missed cycle plus scheduling jitter before calling a snapshot stale.
FRESHNESS_SLACK = 2.0


def _positive_float(store, key: str, default: float) -> float:
    """A settings float, defaulting on absent/malformed/non-positive values AND on a store
    that raises — freshness must never be decided by an exception."""
    try:
        raw = store.get_setting(key)
    except Exception:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _sweep_enabled(store) -> bool:
    try:
        return ((store.get_setting("health_enabled") or "1") == "1"
                and (store.get_setting("health_sweep_enabled") or "1") == "1")
    except Exception:
        return False


def active_freshness_ttl(store) -> float:
    """How old the ACTIVE node's snapshot may be. Derived from the cadence that actually
    refreshes it (`health_active_interval`, driven by the liveness loop)."""
    return max(DEFAULT_FRESHNESS_TTL,
               _positive_float(store, "health_active_interval", DEFAULT_ACTIVE_INTERVAL)
               * FRESHNESS_SLACK)


def standby_freshness_ttl(store) -> float | None:
    """How old a STANDBY's snapshot may be, or None for "don't gate on freshness at all".

    A standby's `checked_at` is refreshed only by the all-node sweep, whose cadence is
    `health_interval` (1800 s by default) — an order of magnitude slower than the active
    node's. Judging standbys by the active node's 180 s TTL made every standby stale ~90 %
    of the time and left auto-failover with no candidate exactly when it needed one. Tie the
    gate to the cadence that actually feeds it instead.

    With the sweep off nothing refreshes standbys at all, so a freshness gate would make
    failover permanently impossible; the pre-promotion real-request preflight in
    `failover.run` is the (stronger) evidence in that configuration.
    """
    if not _sweep_enabled(store):
        return None
    return max(active_freshness_ttl(store),
               _positive_float(store, "health_interval", DEFAULT_SWEEP_INTERVAL) * FRESHNESS_SLACK)


def health_score(h) -> tuple[int, int]:
    """Rank a node by its health snapshot: real-ok > http-ok > tcp-ok > unknown, with a
    lower-latency tie-break. `h` is a NodeHealth or None."""
    if h is None:
        return (0, 0)
    # explicit None check, not `or`: a genuine 0 ms reading (sub-ms int-truncated) is falsy and
    # would otherwise collapse to the 10**9 worst-case penalty, ranking the fastest node last.
    def lat(v):
        return -(v if v is not None else 10**9)
    if h.last_real_ok:
        return (3, lat(h.last_real_ms))
    if h.last_http_ok:
        return (2, lat(h.last_http_ms))
    if h.last_tcp_ok:
        return (1, lat(h.last_tcp_ms))
    return (0, 0)


def _alive(h) -> bool:
    return bool(h and (h.last_real_ok or h.last_http_ok or h.last_tcp_ok))


def checked_at_age_seconds(checked_at: str | None, now: float | None = None) -> float | None:
    if not checked_at:
        return None
    try:
        checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    current = datetime.fromtimestamp(now, timezone.utc) if now is not None else datetime.now(timezone.utc)
    return max(0.0, (current - checked).total_seconds())


def health_age_seconds(h, now: float | None = None) -> float | None:
    return checked_at_age_seconds(h.checked_at if h is not None else None, now)


def health_fresh(h, now: float | None, freshness_ttl: float) -> bool:
    age = health_age_seconds(h, now)
    return age is not None and age <= freshness_ttl


def _rank_key(h, max_fail_count: int | None):
    """`health_score`, but a candidate carrying its OWN sustained-failure evidence sorts
    last. Leaving the active node needs `hysteresis` consecutive failures; without this a
    candidate with just as many failures could be promoted on one lucky preflight."""
    trusted = 1
    if max_fail_count is not None and h is not None and h.fail_count >= max_fail_count:
        trusted = 0
    return (trusted, *health_score(h))


def ranked_nodes(nodes, health: dict, exclude_id=None, require_alive: bool = False,
                 *, now: float | None = None, freshness_ttl: float | None = None,
                 exclude_ids=(), max_fail_count: int | None = None):
    excluded = {exclude_id, *exclude_ids}
    cands = [n for n in nodes if not getattr(n, "stale", False) and n.id not in excluded]
    if freshness_ttl is not None:
        cands = [n for n in cands if health_fresh(health.get(n.id), now, freshness_ttl)]
    if require_alive:
        cands = [n for n in cands if _alive(health.get(n.id))]
    return sorted(cands, key=lambda n: _rank_key(health.get(n.id), max_fail_count), reverse=True)


def best_node(nodes, health: dict, exclude_id=None, require_alive: bool = False,
              *, now: float | None = None, freshness_ttl: float | None = None,
              exclude_ids=(), max_fail_count: int | None = None):
    """The healthiest node in `nodes`, skipping stale ones and `exclude_id`. With
    `require_alive`, only nodes with at least one ok probe are eligible (failover must not
    move to a dead node). Returns the Node or None. `health` maps node_id → NodeHealth."""
    cands = ranked_nodes(nodes, health, exclude_id, require_alive,
                         now=now, freshness_ttl=freshness_ttl,
                         exclude_ids=exclude_ids, max_fail_count=max_fail_count)
    if not cands:
        return None
    return cands[0]
