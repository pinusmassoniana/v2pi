from pi_gw_panel.controller import apply_node
from pi_gw_panel.health.selection import best_node

DEFAULT_HYSTERESIS = 3
DEFAULT_COOLDOWN = 120.0


def decide(health: dict, nodes: list, active_id, hysteresis: int, cooldown: float,
           now: float, last_failover_at):
    """Pure failover decision → the node_id to fail over to, or None.

    Fires only when the active node's consecutive real-request failures have reached
    `hysteresis` AND we're past the `cooldown` debounce window since the last failover.
    The candidate is the *healthiest* alive node other than the active one, skipping stale
    nodes (NC3: real > http > tcp, lowest latency). `health` maps node_id → NodeHealth."""
    if active_id is None:
        return None
    active_h = health.get(active_id)
    if active_h is None or active_h.fail_count < hysteresis:
        return None
    if last_failover_at is not None and (now - last_failover_at) < cooldown:
        return None
    cand = best_node(nodes, health, exclude_id=active_id, require_alive=True)
    return cand.id if cand is not None else None


def run(state, now: float, apply_fn=apply_node):
    """Evaluate persisted health and, if warranted, fail the active node over to a
    TCP-alive candidate via `apply_node`. Gated by the `failover_enabled` setting.
    Returns the new active node_id on a successful switch, else None.

    Cascade + real-request verification are emergent: after a switch the next monitor
    tick real-probes the new active and accumulates its own `fail_count`, so a still-bad
    path fails over again on a later tick (bounded by `cooldown`, so no thrash)."""
    store = state.store
    if (store.get_setting("failover_enabled") or "1") != "1":
        return None
    hysteresis = int(store.get_setting("health_hysteresis") or DEFAULT_HYSTERESIS)
    cooldown = float(store.get_setting("failover_cooldown") or DEFAULT_COOLDOWN)
    nodes = store.list_nodes()
    health = {h.node_id: h for h in store.list_health()}
    active_v = store.get_setting("active_node_id")
    active_id = int(active_v) if active_v else None
    last_v = store.get_setting("last_failover_at")
    last_failover_at = float(last_v) if last_v else None

    candidate = decide(health, nodes, active_id, hysteresis, cooldown, now, last_failover_at)
    if candidate is None:
        return None
    node = store.get_node(candidate)
    res = apply_fn(node, state.settings, state.supervisor, state.net, store=store)
    if not res.ok:
        return None
    store.set_setting("last_failover_at", str(now))
    return candidate
