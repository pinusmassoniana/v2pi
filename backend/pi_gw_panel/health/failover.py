import logging
from datetime import datetime, timezone

from pi_gw_panel.controller import apply_node, apply_lock
from pi_gw_panel.health import probe
from pi_gw_panel.health.selection import (
    DEFAULT_FRESHNESS_TTL, active_freshness_ttl, best_node, health_fresh, ranked_nodes,
    standby_freshness_ttl,
)
from pi_gw_panel.net_control import events

log = logging.getLogger("pi_gw_panel")
DEFAULT_HYSTERESIS = 3
DEFAULT_COOLDOWN = 120.0
PREFLIGHT_TIMEOUT = 5.0
# A node we just fled sits out this long. Leaving it took `hysteresis` consecutive failures;
# returning to it must not take one 5 s preflight, or a half-broken node ping-pongs the whole
# LAN back and forth at the cooldown rate.
DEMOTION_GRACE = 600.0
# Each preflight spins a throwaway xray and can burn PREFLIGHT_TIMEOUT. Bound how many run in
# one evaluation so a large (or entirely dead) pool can't stall the liveness loop for minutes.
MAX_PREFLIGHTS = 5


def _int_setting(store, key: str) -> int | None:
    try:
        raw = store.get_setting(key)
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _recently_demoted(store, now: float, last_failover_at) -> tuple[int, ...]:
    """The node the last failover moved AWAY from, while its negative-affinity window holds."""
    demoted = _int_setting(store, "last_demoted_node_id")
    if demoted is None or last_failover_at is None:
        return ()
    return (demoted,) if (now - last_failover_at) < DEMOTION_GRACE else ()


def _maybe_report_all_down(store, health, nodes, active_id, hysteresis, cooldown, now,
                           last_failover_at, *, candidates_exhausted: bool = False,
                           active_ttl: float = DEFAULT_FRESHNESS_TTL,
                           standby_ttl: float | None = DEFAULT_FRESHNESS_TTL,
                           exclude_ids=()):
    """Emit an 'all-nodes-down' event when the active node is past the failover threshold but there
    is no alive node to move to — the one scenario the operator most needs to see, otherwise silent.
    Rate-limited to once per cooldown so it doesn't flood the event log every tick."""
    if active_id is None:
        return
    ah = health.get(active_id)
    if ah is None or ah.fail_count < hysteresis:
        return
    if not health_fresh(ah, now, active_ttl):
        return
    if last_failover_at is not None and (now - last_failover_at) < cooldown:
        return
    if (not candidates_exhausted and
            best_node(nodes, health, exclude_id=active_id, require_alive=standby_ttl is not None,
                      now=now, freshness_ttl=standby_ttl, exclude_ids=exclude_ids) is not None):
        return   # a candidate exists → decide() would have returned it, not None
    last_v = store.get_setting("last_all_down_at")
    if last_v and (now - float(last_v)) < cooldown:
        return
    store.set_setting("last_all_down_at", str(now))
    events.record(store, "all-nodes-down", "active node failing and no alive node to fail over to", now=now)


_INHERIT = object()


def decide(health: dict, nodes: list, active_id, hysteresis: int, cooldown: float,
           now: float, last_failover_at, *, freshness_ttl: float | None = None,
           standby_freshness_ttl=_INHERIT, exclude_ids=(), require_alive: bool = True):
    """Pure failover decision → the node_id to fail over to, or None.

    Fires only when the active node's consecutive real-request failures have reached
    `hysteresis` AND we're past the `cooldown` debounce window since the last failover.
    The candidate is the *healthiest* alive node other than the active one, skipping stale
    nodes (NC3: real > http > tcp, lowest latency). `health` maps node_id → NodeHealth.

    `freshness_ttl` gates the ACTIVE node's snapshot; `standby_freshness_ttl` gates the
    candidates' (they are refreshed by a much slower loop, so they need their own budget —
    None disables the standby gate entirely). Candidates carrying `hysteresis` failures of
    their own sort last rather than being trusted like a clean node."""
    if standby_freshness_ttl is _INHERIT:
        standby_freshness_ttl = freshness_ttl
    if active_id is None:
        return None
    active_h = health.get(active_id)
    if active_h is None or active_h.fail_count < hysteresis:
        return None
    if freshness_ttl is not None and not health_fresh(active_h, now, freshness_ttl):
        return None
    if last_failover_at is not None and (now - last_failover_at) < cooldown:
        return None
    cand = best_node(nodes, health, exclude_id=active_id, require_alive=require_alive,
                     now=now, freshness_ttl=standby_freshness_ttl,
                     exclude_ids=exclude_ids, max_fail_count=hysteresis)
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
    # The two loops refresh health at very different cadences, so they get separate budgets.
    active_ttl = active_freshness_ttl(store)
    standby_ttl = standby_freshness_ttl(store)
    # With the sweep off nothing ever marks a standby "alive"; the preflight below is then the
    # only (and stronger) evidence, so don't also demand a probe result that can't exist.
    require_alive = standby_ttl is not None
    demoted = _recently_demoted(store, now, last_failover_at)
    if demoted:
        log.debug("failover: node %s stays out — post-demotion grace window", demoted[0])

    candidate = decide(
        health, nodes, active_id, hysteresis, cooldown, now, last_failover_at,
        freshness_ttl=active_ttl, standby_freshness_ttl=standby_ttl,
        exclude_ids=demoted, require_alive=require_alive,
    )
    if candidate is None:
        _maybe_report_all_down(store, health, nodes, active_id, hysteresis, cooldown, now,
                               last_failover_at, active_ttl=active_ttl,
                               standby_ttl=standby_ttl, exclude_ids=demoted)
        return None
    candidates = ranked_nodes(
        nodes, health, exclude_id=active_id, require_alive=require_alive,
        now=now, freshness_ttl=standby_ttl, exclude_ids=demoted, max_fail_count=hysteresis,
    )[:MAX_PREFLIGHTS]
    checked_at = datetime.fromtimestamp(now, timezone.utc).isoformat()
    probe_url = store.get_setting("health_probe_url") or "https://api.ipify.org?format=json"
    for node in candidates:
        with apply_lock:
            cur_v = store.get_setting("active_node_id")
            if (int(cur_v) if cur_v else None) != active_id:
                return None
        try:
            # Same provenance the health sweep probes by: an operator's own node on the LAN is
            # marked alive by that sweep and ranked here, but a strict preflight refuses its
            # address and `not real_ok` drops it — so without this it is selectable in theory
            # and unpromotable in practice. A feed-imported candidate stays strict.
            real_ok, real_ms, egress, egress6 = real_through(
                node, state.xray_bin, probe_url, timeout=PREFLIGHT_TIMEOUT,
                allow_private=probe.operator_added(node),
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
            # Remember what we fled so the next evaluation can't hand the LAN straight back to
            # it on one successful preflight (see DEMOTION_GRACE).
            store.set_setting("last_demoted_node_id", str(active_id) if active_id is not None else "")
            return node.id
    _maybe_report_all_down(
        store, health, nodes, active_id, hysteresis, cooldown, now, last_failover_at,
        candidates_exhausted=True, active_ttl=active_ttl, standby_ttl=standby_ttl,
        exclude_ids=demoted,
    )
    return None
