"""Priority failover: an ordered list of nodes (from any subscription or manual server) that the
service fails over along, top to bottom, and climbs back up when a higher one has been healthy
long enough.

The list lives in the settings k/v as comma-separated node ids (`failover_order`); a non-empty list
switches auto-failover from "healthiest node anywhere" to this. Nodes outside the list are never
picked automatically — with every list node down the service stays where it is.

Every list node except the active one gets a real request (throwaway xray) every
`failover_real_interval` seconds; the active node keeps its own liveness probe. `failover_ok_since`
remembers, per node, when its current unbroken run of successful real checks began — failback to a
higher node needs that run to be at least `failback_minutes` long.
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor

from pi_gw_panel.health import probe
from pi_gw_panel.health.selection import health_fresh
from pi_gw_panel.net_control import events

log = logging.getLogger("pi_gw_panel")
ORDER_KEY = "failover_order"
OK_SINCE_KEY = "failover_ok_since"
DEFAULT_FAILBACK_MIN = 20
DEFAULT_REAL_INTERVAL = 60
DEFAULT_PROBE_BATCH = 3
PROBE_TIMEOUT = 8.0
# probe.real_through_node caps concurrent throwaway xrays at 3 itself; match it.
PROBE_WORKERS = 3


def _int(store, key: str, default: int) -> int:
    try:
        return int(store.get_setting(key) or default)
    except (TypeError, ValueError):
        return default


def get_order(store) -> list[int]:
    raw = store.get_setting(ORDER_KEY) or ""
    out: list[int] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if tok.isdigit() and int(tok) not in out:
            out.append(int(tok))
    return out


def set_order(store, ids: list[int]) -> None:
    store.set_setting(ORDER_KEY, ",".join(str(i) for i in ids))


def live_order(store) -> list[int]:
    """The stored order minus nodes that have since been deleted."""
    return [i for i in get_order(store) if store.get_node(i) is not None]


def ok_since(store) -> dict[int, float]:
    try:
        raw = json.loads(store.get_setting(OK_SINCE_KEY) or "{}")
        return {int(k): float(v) for k, v in raw.items()}
    except (TypeError, ValueError, AttributeError):
        return {}


def note_real(store, node_id: int, ok: bool, now: float) -> None:
    """Start (on the first success) or break (on any failure) a node's healthy run."""
    since = ok_since(store)
    if ok:
        if node_id in since:
            return
        since[node_id] = now
    elif since.pop(node_id, None) is None:
        return
    store.set_setting(OK_SINCE_KEY, json.dumps({str(k): v for k, v in since.items()}))


def real_interval(store) -> float:
    return float(max(10, _int(store, "failover_real_interval", DEFAULT_REAL_INTERVAL)))


def probe_batch(store) -> int:
    return max(1, _int(store, "failover_probe_batch", DEFAULT_PROBE_BATCH))


def due_nodes(order: list[int], active_id, last_probed: dict[int, float], now: float,
              interval: float, batch: int) -> list[int]:
    """The next batch to check: list nodes (not the active one) whose last check is at least
    `interval` old, longest-waiting first. Spreads the checks across liveness ticks instead of
    starting a throwaway xray for every node at once."""
    due = [n for n in order if n != active_id and now - last_probed.get(n, 0.0) >= interval]
    due.sort(key=lambda n: last_probed.get(n, 0.0))
    return due[:batch]


def fresh_ttl(store) -> float:
    # tolerate two missed rounds plus a slow round of timeouts
    return max(180.0, real_interval(store) * 3)


def decide(order: list[int], health: dict, active_id, since: dict[int, float], now: float, *,
           hysteresis: int, failback_sec: float, ttl: float) -> int | None:
    """Pure decision → the node to switch to, or None.

    - Active node failed (`hysteresis` consecutive real failures): the FIRST available list node,
      top-down, skipping the active one — with #2 and #3 down that is #4 straight away.
    - Active node healthy: the highest list node ABOVE it whose healthy run has lasted
      `failback_sec`. A node outside the list ranks below every list node, so a manual pick off
      the list is followed back onto it.
    - Disconnected (no active node): nothing — the operator stopped the tunnel."""
    if active_id is None:
        return None

    def available(nid: int) -> bool:
        h = health.get(nid)
        return (h is not None and bool(h.last_real_ok) and h.fail_count == 0
                and health_fresh(h, now, ttl))

    ah = health.get(active_id)
    if ah is not None and ah.fail_count >= hysteresis:
        return next((n for n in order if n != active_id and available(n)), None)
    rank = order.index(active_id) if active_id in order else len(order)
    for n in order[:rank]:
        if available(n) and n in since and now - since[n] >= failback_sec:
            return n
    return None


def probe_list(state, now: float, now_iso: str, real_through=probe.real_through_node,
               commit=None, last_probed: dict[int, float] | None = None) -> None:
    """Real-check the next due batch of list nodes (see `due_nodes`); `last_probed` is updated in
    place. `commit(fn)` runs the store write; the liveness loop passes one that holds its locks
    and drops writes after stop()."""
    store = state.store
    last_probed = {} if last_probed is None else last_probed
    active_v = store.get_setting("active_node_id")
    active_id = int(active_v) if active_v else None
    ids = due_nodes(live_order(store), active_id, last_probed, now, real_interval(store),
                    probe_batch(store))
    for i in ids:
        last_probed[i] = now
    nodes = [n for n in (store.get_node(i) for i in ids) if n is not None]
    if not nodes:
        return
    probe_url = store.get_setting("health_probe_url") or "https://api.ipify.org?format=json"

    def one(node):
        try:
            return node, real_through(node, state.xray_bin, probe_url, timeout=PROBE_TIMEOUT,
                                      allow_private=probe.operator_added(node))
        except Exception:
            log.warning("priority: the real check of node %s could not run", node.id, exc_info=True)
            return node, None

    with ThreadPoolExecutor(max_workers=PROBE_WORKERS, thread_name_prefix="priority-probe") as ex:
        results = list(ex.map(one, nodes))

    def write():
        for node, res in results:
            if res is None:
                continue
            real_ok, real_ms, egress, egress6 = res
            store.update_health_real(node.id, real_ok=real_ok, real_ms=real_ms, egress_ip=egress,
                                     egress_ip6=egress6, checked_at=now_iso)
            note_real(store, node.id, real_ok, now)

    (commit or (lambda fn: fn()))(write)


def run(state, now: float, apply_fn, apply_lock) -> int | None:
    """Evaluate the priority list and switch if `decide` says so. Returns the new node id."""
    store = state.store
    order = live_order(store)
    active_v = store.get_setting("active_node_id")
    active_id = int(active_v) if active_v else None
    hysteresis = _int(store, "health_hysteresis", 3)
    health = {h.node_id: h for h in store.list_health()}
    target = decide(order, health, active_id, ok_since(store), now, hysteresis=hysteresis,
                    failback_sec=_int(store, "failback_minutes", DEFAULT_FAILBACK_MIN) * 60,
                    ttl=fresh_ttl(store))
    if target is None:
        ah = health.get(active_id) if active_id is not None else None
        if ah is not None and ah.fail_count >= hysteresis:
            _report_all_down(store, now)
        return None
    node = store.get_node(target)
    with apply_lock:
        cur = store.get_setting("active_node_id")
        if (int(cur) if cur else None) != active_id or node is None:
            return None
        res = apply_fn(node, state.settings, state.supervisor, state.net, store=store)
    if not res.ok:
        log.warning("priority: switching to node %s failed: %s", target, res.error)
        return None
    store.set_setting("last_failover_at", str(now))
    return target


def _report_all_down(store, now: float) -> None:
    """One event per 10 minutes while the active node is dead and no list node is up."""
    last = store.get_setting("last_all_down_at")
    try:
        if last and now - float(last) < 600:
            return
    except ValueError:
        pass
    store.set_setting("last_all_down_at", str(now))
    events.record(store, "all-nodes-down",
                  "active node failing and no node in the failover list is up", now=now)
