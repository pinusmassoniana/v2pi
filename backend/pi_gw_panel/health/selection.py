"""Shared "pick the healthiest node" scoring, used by both auto-failover and the
manual connect-best route so the two never disagree (audit NC3)."""


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


def best_node(nodes, health: dict, exclude_id=None, require_alive: bool = False):
    """The healthiest node in `nodes`, skipping stale ones and `exclude_id`. With
    `require_alive`, only nodes with at least one ok probe are eligible (failover must not
    move to a dead node). Returns the Node or None. `health` maps node_id → NodeHealth."""
    cands = [n for n in nodes if not getattr(n, "stale", False) and n.id != exclude_id]
    if require_alive:
        cands = [n for n in cands if _alive(health.get(n.id))]
    if not cands:
        return None
    return max(cands, key=lambda n: health_score(health.get(n.id)))
