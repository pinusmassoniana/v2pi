from datetime import datetime, timezone

from pi_gw_panel.controller import apply_node, apply_lock
from pi_gw_panel.health import probe
from pi_gw_panel.health.selection import (
    DEFAULT_FRESHNESS_TTL, best_node, health_fresh, ranked_nodes,
)
from pi_gw_panel.net_control import events

DEFAULT_HYSTERESIS = 3
DEFAULT_COOLDOWN = 120.0
PREFLIGHT_TIMEOUT = 5.0


def _maybe_report_all_down(store, health, nodes, active_id, hysteresis, cooldown, now,
                           last_failover_at, *, candidates_exhausted: bool = False):
    """Emit an 'all-nodes-down' event when the active node is past the failover threshold but there
    is no alive node to move to — the one scenario the operator most needs to see, otherwise silent.
    Rate-limited to once per cooldown so it doesn't flood the event log every tick."""
    if active_id is None:
        return
    ah = health.get(active_id)
    if ah is None or ah.fail_count < hysteresis:
        return
    if not health_fresh(ah, now, DEFAULT_FRESHNESS_TTL):
        return
    if last_failover_at is not None and (now - last_failover_at) < cooldown:
        return
    if (not candidates_exhausted and
            best_node(nodes, health, exclude_id=active_id, require_alive=True,
                      now=now, freshness_ttl=DEFAULT_FRESHNESS_TTL) is not None):
        return   # a candidate exists → decide() would have returned it, not None
    last_v = store.get_setting("last_all_down_at")
    if last_v and (now - float(last_v)) < cooldown:
        return
    store.set_setting("last_all_down_at", str(now))
    events.record(store, "all-nodes-down", "active node failing and no alive node to fail over to", now=now)


def decide(health: dict, nodes: list, active_id, hysteresis: int, cooldown: float,
           now: float, last_failover_at, *, freshness_ttl: float | None = None):
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
    if freshness_ttl is not None and not health_fresh(active_h, now, freshness_ttl):
        return None
    if last_failover_at is not None and (now - last_failover_at) < cooldown:
        return None
    cand = best_node(nodes, health, exclude_id=active_id, require_alive=True,
                     now=now, freshness_ttl=freshness_ttl)
    return cand.id if cand is not None else None


def run(state, now: float, apply_fn=apply_node, real_through=probe.real_through_node):
    """Evaluate persisted health and, if warranted, fail the active node over to a
    TCP-alive candidate via `apply_node`. Gated by the `failover_enabled` setting.
    Returns the new active node_id on a successful switch, else None.

    Candidates require fresh health and pass a throwaway-Xray real request before apply;
    a failed preflight/apply falls through to the next ranked candidate."""
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

    candidate = decide(
        health, nodes, active_id, hysteresis, cooldown, now, last_failover_at,
        freshness_ttl=DEFAULT_FRESHNESS_TTL,
    )
    if candidate is None:
        _maybe_report_all_down(store, health, nodes, active_id, hysteresis, cooldown, now, last_failover_at)
        return None
    candidates = ranked_nodes(
        nodes, health, exclude_id=active_id, require_alive=True,
        now=now, freshness_ttl=DEFAULT_FRESHNESS_TTL,
    )
    checked_at = datetime.fromtimestamp(now, timezone.utc).isoformat()
    probe_url = store.get_setting("health_probe_url") or "https://api.ipify.org?format=json"
    for node in candidates:
        with apply_lock:
            cur_v = store.get_setting("active_node_id")
            if (int(cur_v) if cur_v else None) != active_id:
                return None
        try:
            real_ok, real_ms, egress, egress6 = real_through(
                node, state.xray_bin, probe_url, timeout=PREFLIGHT_TIMEOUT,
            )
        except Exception:
            real_ok, real_ms, egress, egress6 = False, None, None, None
        with apply_lock:
            cur_v = store.get_setting("active_node_id")
            if (int(cur_v) if cur_v else None) != active_id:
                return None
            store.update_health_real(
                node.id, real_ok=real_ok, real_ms=real_ms, egress_ip=egress,
                egress_ip6=egress6, checked_at=checked_at,
            )
            if not real_ok:
                continue
            res = apply_fn(node, state.settings, state.supervisor, state.net, store=store)
        if res.ok:
            store.set_setting("last_failover_at", str(now))
            return node.id
    _maybe_report_all_down(
        store, health, nodes, active_id, hysteresis, cooldown, now, last_failover_at,
        candidates_exhausted=True,
    )
    return None
