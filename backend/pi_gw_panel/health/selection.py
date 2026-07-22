"""Shared "pick the healthiest node" scoring, used by both auto-failover and the
manual connect-best route so the two never disagree (audit NC3)."""
from datetime import datetime, timezone

DEFAULT_FRESHNESS_TTL = 180.0


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


def ranked_nodes(nodes, health: dict, exclude_id=None, require_alive: bool = False,
                 *, now: float | None = None, freshness_ttl: float | None = None):
    cands = [n for n in nodes if not getattr(n, "stale", False) and n.id != exclude_id]
    if freshness_ttl is not None:
        cands = [n for n in cands if health_fresh(health.get(n.id), now, freshness_ttl)]
    if require_alive:
        cands = [n for n in cands if _alive(health.get(n.id))]
    return sorted(cands, key=lambda n: health_score(health.get(n.id)), reverse=True)


def best_node(nodes, health: dict, exclude_id=None, require_alive: bool = False,
              *, now: float | None = None, freshness_ttl: float | None = None):
    """The healthiest node in `nodes`, skipping stale ones and `exclude_id`. With
    `require_alive`, only nodes with at least one ok probe are eligible (failover must not
    move to a dead node). Returns the Node or None. `health` maps node_id → NodeHealth."""
    cands = ranked_nodes(nodes, health, exclude_id, require_alive,
                         now=now, freshness_ttl=freshness_ttl)
    if not cands:
        return None
    return cands[0]
