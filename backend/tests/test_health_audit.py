"""Regression cover for the failure modes that silently disable auto-failover.

Every test here fails if the corresponding defect comes back: a dead loop task, a standby that
is never fresh enough to be promoted, a node re-promoted the instant it is demoted, a probe
dialling the LAN on a hostile feed's behalf, a crash that rewinds a completed failover, and a
crash-looping watchdog that erases the event history it is supposed to write into.
"""
import asyncio
import threading
import time
from datetime import datetime, timezone

import pytest

from pi_gw_panel.config import Settings
from pi_gw_panel.controller import ApplyResult, apply_node, reapply_active_node
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.health import failover, probe
from pi_gw_panel.health.liveness import LivenessLoop
from pi_gw_panel.health.monitor import DEFAULT_INTERVAL, HealthMonitor
from pi_gw_panel.health.selection import (
    DEFAULT_FRESHNESS_TTL, active_freshness_ttl, standby_freshness_ttl,
)
from pi_gw_panel.health.snapshot import active_health, health_status
from pi_gw_panel.models import Node, NodeHealth
from pi_gw_panel.net_control import events as conn_events
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.nodes.store import NodeStore


def _ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _store(settings=None):
    conn = connect(settings.db_path if settings else ":memory:", check_same_thread=False)
    init_schema(conn)
    return NodeStore(conn)


class _FakeSup:
    def __init__(self, state="working", comes_up=False):
        self._state = state
        self._comes_up = comes_up
        self.started = 0

    def state(self):
        return self._state

    def start(self):
        self.started += 1
        if self._comes_up:
            self._state = "working"

    def status(self):
        return {"running": self._state == "working"}


class _State:
    def __init__(self, store, settings=None, sup=None):
        self.store = store
        self.settings = settings or Settings()
        self.supervisor = sup or _FakeSup()
        self.net = DryRunBackend()
        self.xray_bin = self.settings.xray_bin


class _ExplodingStore:
    """A store whose `get_setting` raises — the transient-DB-error shape that used to end the
    loop tasks for good. Everything else is delegated to a working store."""

    def __init__(self, inner):
        self._inner = inner
        self.calls = 0

    def get_setting(self, key):
        self.calls += 1
        raise RuntimeError("database is locked")

    def __getattr__(self, name):
        return getattr(self._inner, name)


# --- 1. a raising store must not end either loop -----------------------------------------

def test_liveness_loop_survives_a_store_exception_in_the_tick():
    state = _State(_ExplodingStore(_store()), sup=_FakeSup("working"))
    ticks = []

    async def drive():
        loop = LivenessLoop(state, interval_sec=0.01,
                            failover_run=lambda st, now: ticks.append(now))
        loop.start()
        await asyncio.sleep(0.12)
        task = loop._task
        alive = task is not None and not task.done()
        await loop.stop()
        return alive

    assert asyncio.run(drive()) is True     # the task must still be running
    assert len(ticks) >= 2                  # ...and still evaluating failover every tick


def test_liveness_probe_interval_defaults_instead_of_raising():
    state = _State(_ExplodingStore(_store()))
    from pi_gw_panel.health.liveness import DEFAULT_PROBE_INTERVAL
    assert LivenessLoop(state)._probe_interval() == DEFAULT_PROBE_INTERVAL


def test_health_monitor_loop_survives_a_store_exception_in_the_tick():
    state = _State(_ExplodingStore(_store()))
    sweeps = []

    async def drive():
        monitor = HealthMonitor(state, tick_sec=0.01,
                                tcp_ping=lambda *_a, **_k: (True, 1),
                                http_ping=lambda *_a, **_k: (True, 1),
                                real_request=lambda *_a, **_k: (True, 200, 1, None),
                                after_tick=lambda: sweeps.append(1))
        monitor.start()
        await asyncio.sleep(0.12)
        task = monitor._task
        alive = task is not None and not task.done()
        await monitor.stop()
        return alive

    assert asyncio.run(drive()) is True


@pytest.mark.parametrize("raw", ["nonsense", "", "0", None])
def test_health_monitor_interval_never_raises_on_a_bad_setting(raw):
    """`_interval()` runs OUTSIDE `_safe_tick`; a malformed value used to kill the sweep task
    permanently — a second, independent way to disable failover-to-standby."""
    store = _store()
    if raw is not None:
        store.set_setting("health_interval", raw)
    interval = HealthMonitor(_State(store))._interval()
    assert interval >= 5.0
    assert interval == DEFAULT_INTERVAL or raw == "0"


def test_health_monitor_interval_survives_a_raising_store():
    assert HealthMonitor(_State(_ExplodingStore(_store())))._interval() == DEFAULT_INTERVAL


# --- 2. with DEFAULT settings a standby is fresh when failover needs it -------------------

def test_default_settings_make_a_sweep_aged_standby_eligible():
    """The sweep runs every 1800 s by default and is the ONLY thing that refreshes a standby's
    `checked_at`; judging standbys by the active node's 180 s budget left them ineligible ~90 %
    of every cycle — exactly the state auto-failover finds them in."""
    store = _store()
    assert active_freshness_ttl(store) == DEFAULT_FRESHNESS_TTL
    assert standby_freshness_ttl(store) >= 1800.0

    now = 100_000.0
    active = store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u1"))
    standby = store.add_node(Node(id=None, name="b", address="2.2.2.2", port=443, uuid="u2"))
    store.set_setting("active_node_id", str(active))
    store.upsert_health(NodeHealth(node_id=active, last_real_ok=False, fail_count=3,
                                   checked_at=_ts(now - 30)))       # probed 30 s ago
    store.upsert_health(NodeHealth(node_id=standby, last_tcp_ok=True,
                                   checked_at=_ts(now - 1500)))     # last full sweep, 25 min ago

    # the panel must promise exactly what failover is about to do
    assert health_status(store, now=now)["eligible_standby_count"] == 1

    state = _State(store)
    applied = []

    def fake_apply(node, *_a, store=None, **_k):
        applied.append(node.id)
        store.set_setting("active_node_id", str(node.id))
        return ApplyResult(ok=True)

    result = failover.run(state, now, apply_fn=fake_apply,
                          real_through=lambda *_a, **_k: (True, 9, "9.9.9.9", None))
    assert result == standby and applied == [standby]


def test_standby_gate_lifts_when_the_sweep_is_disabled():
    """With `health_sweep_enabled=0` nothing ever refreshes a standby, so a freshness gate
    would make failover impossible — while Settings tells the operator it still works. The
    pre-promotion preflight is the evidence in that configuration."""
    store = _store()
    store.set_setting("health_sweep_enabled", "0")
    assert standby_freshness_ttl(store) is None

    now = 100_000.0
    active = store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u1"))
    standby = store.add_node(Node(id=None, name="b", address="2.2.2.2", port=443, uuid="u2"))
    store.set_setting("active_node_id", str(active))
    store.upsert_health(NodeHealth(node_id=active, last_real_ok=False, fail_count=3,
                                   checked_at=_ts(now - 30)))
    # the standby has NO health row at all — the sweep never ran

    preflighted = []

    def real_through(node, *_a, **_k):
        preflighted.append(node.id)
        return True, 12, "203.0.113.9", None

    assert failover.run(_State(store), now,
                        apply_fn=lambda node, *_a, **_k: ApplyResult(ok=True),
                        real_through=real_through) == standby
    assert preflighted == [standby]


def test_active_health_snapshot_agrees_with_health_status_on_freshness():
    """FIX-E-3: `active_health()` used to judge staleness against a fixed 180 s constant while
    `health_status()` (and failover) derive the budget from `health_active_interval` — any
    interval configured above 90 s made the two disagree about the exact same snapshot. Pin a
    checked_at age that is stale under the old fixed constant but fresh under the configured
    interval, and require both call sites to now report the same answer."""
    store = _store()
    store.set_setting("health_active_interval", "120")     # ttl = max(180, 120*2) = 240 s
    assert active_freshness_ttl(store) == 240.0

    nid = store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u1"))
    store.set_setting("active_node_id", str(nid))
    # 200 s old: past the old fixed 180 s threshold, inside the configured 240 s budget.
    checked_at = _ts(time.time() - 200)
    store.upsert_health(NodeHealth(node_id=nid, last_real_ok=True, last_real_ms=5,
                                   egress_ip="1.2.3.4", checked_at=checked_at))

    assert health_status(store)["active_health_fresh"] is True
    assert active_health(store)["stale"] is False


# --- 3. the node just demoted is not re-promoted on one good preflight --------------------

def test_demoted_node_is_not_immediately_re_promoted():
    """Leaving a node takes `hysteresis` consecutive failures; returning to it must not take a
    single 5 s preflight, or a half-broken node ping-pongs the LAN at the cooldown rate."""
    store = _store()
    first = store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u1"))
    second = store.add_node(Node(id=None, name="b", address="2.2.2.2", port=443, uuid="u2"))
    now = 100_000.0
    store.set_setting("active_node_id", str(first))
    store.upsert_health(NodeHealth(node_id=first, last_tcp_ok=True, fail_count=3,
                                   checked_at=_ts(now)))
    store.upsert_health(NodeHealth(node_id=second, last_tcp_ok=True, checked_at=_ts(now)))
    state = _State(store)

    def fake_apply(node, *_a, store=None, **_k):
        store.set_setting("active_node_id", str(node.id))
        return ApplyResult(ok=True)

    def ok_preflight(*_a, **_k):
        return True, 5, "9.9.9.9", None

    assert failover.run(state, now, apply_fn=fake_apply, real_through=ok_preflight) == second
    assert store.get_setting("last_demoted_node_id") == str(first)

    # the new active immediately looks bad too, and the demoted node preflights fine
    later = now + failover.DEFAULT_COOLDOWN + 1
    store.upsert_health(NodeHealth(node_id=second, last_tcp_ok=True, fail_count=3,
                                   checked_at=_ts(later)))
    store.upsert_health(NodeHealth(node_id=first, last_tcp_ok=True, checked_at=_ts(later)))
    assert failover.run(state, later, apply_fn=fake_apply, real_through=ok_preflight) is None
    assert store.get_setting("active_node_id") == str(second)

    # ...but once the negative-affinity window has passed it is a candidate again
    much_later = now + failover.DEMOTION_GRACE + 1
    store.upsert_health(NodeHealth(node_id=second, last_tcp_ok=True, fail_count=3,
                                   checked_at=_ts(much_later)))
    store.upsert_health(NodeHealth(node_id=first, last_tcp_ok=True, checked_at=_ts(much_later)))
    assert failover.run(state, much_later, apply_fn=fake_apply,
                        real_through=ok_preflight) == first


def test_candidate_with_its_own_failure_streak_ranks_below_a_clean_one():
    health = {
        1: NodeHealth(node_id=1, last_tcp_ok=True, fail_count=3, checked_at=_ts(1000)),
        2: NodeHealth(node_id=2, last_http_ok=True, last_http_ms=1, fail_count=3,
                      checked_at=_ts(1000)),   # better probe, but failing just as hard
        3: NodeHealth(node_id=3, last_tcp_ok=True, last_tcp_ms=999, checked_at=_ts(1000)),
    }
    nodes = [Node(id=i, name=f"n{i}", address=f"{i}.{i}.{i}.{i}", port=443, uuid=f"u{i}")
             for i in (1, 2, 3)]
    assert failover.decide(health, nodes, 1, 3, 0, 1000, None, freshness_ttl=180) == 3


# --- 4. a private/loopback endpoint is never probed ---------------------------------------

@pytest.mark.parametrize("address", ["127.0.0.1", "192.168.1.1", "10.0.0.5", "169.254.1.1",
                                     "0.0.0.0", "::1", "localhost", ""])
def test_probes_refuse_non_public_node_addresses(address):
    """Node endpoints come from a remote feed; probing them on a timer and reporting the
    outcome through the node-health API turns the panel into an internal port scanner."""
    dialled = []

    def connect(addr, timeout):
        dialled.append(addr)
        raise AssertionError("must not dial a non-public address")

    assert probe.tcp_ping(address, 443, connect=connect) == (False, None)
    assert probe.http_ping(address, 443, "sni", connect=connect) == (False, None)
    node = Node(id=1, name="n", address=address, port=443, uuid="u")
    assert probe.real_through_node(
        node, "xray", "https://probe",
        spawn=lambda _p: (_ for _ in ()).throw(AssertionError("must not spawn xray")),
    ) == (False, None, None, None)
    assert dialled == []


def test_probes_still_allow_public_addresses():
    class _Conn:
        def close(self):
            pass

    seen = []

    def connect(addr, timeout):
        seen.append(addr)
        return _Conn()

    assert probe.tcp_ping("1.2.3.10", 443, connect=connect)[0] is True
    assert seen == [("1.2.3.10", 443)]


def test_health_sweep_does_not_dial_a_private_node(settings):
    store = _store(settings)
    private = store.add_node(Node(id=None, name="lan", address="192.168.1.1", port=443, uuid="u1"))
    public = store.add_node(Node(id=None, name="pub", address="1.2.3.4", port=443, uuid="u2"))
    dialled = []

    def tcp_ping(address, port):
        return probe.tcp_ping(address, port,
                              connect=lambda addr, timeout: dialled.append(addr) or _raise())

    def _raise():
        raise OSError("refused")

    monitor = HealthMonitor(_State(store, settings), tcp_ping=tcp_ping,
                            http_ping=lambda *_a, **_k: (False, None),
                            real_request=lambda *_a, **_k: (False, None, None, None),
                            now_iso=lambda: _ts(1000))
    monitor.run_once()
    assert [addr[0] for addr in dialled] == ["1.2.3.4"]
    assert store.get_health(private).last_tcp_ok is False


# --- 5. a crash between the xray reload and the active_node_id write converges FORWARD ----

class _CrashAfterReload(_FakeSup):
    """Reload succeeds (the live config is now the new node's) and then the panel dies before
    `active_node_id` is written."""

    def __init__(self):
        super().__init__("working")
        self.reloads = 0

    def reload(self):
        self.reloads += 1
        return True


def test_crash_between_reload_and_active_write_converges_forward(settings, stub_xray):
    settings.xray_bin = stub_xray
    store = _store(settings)
    old = store.add_node(Node(id=None, name="old", address="1.2.3.1", port=443, uuid="u1",
                              sni="x", public_key="PK", short_id="ab"))
    new = store.add_node(Node(id=None, name="new", address="1.2.3.2", port=443, uuid="u2",
                              sni="x", public_key="PK", short_id="cd"))
    store.set_setting("active_node_id", str(old))
    state = _State(store, settings, sup=_CrashAfterReload())

    class _Boom(Exception):
        pass

    # crash exactly in the gap: reload done, active_node_id not yet written
    original_set = store.set_setting

    def crashing_set(key, value):
        if key == "active_node_id" and value == str(new):
            raise _Boom("power cut")
        return original_set(key, value)

    store.set_setting = crashing_set
    with pytest.raises(_Boom):
        apply_node(state.store.get_node(new), settings, state.supervisor, state.net,
                   store=store, xray_bin=stub_xray)
    store.set_setting = original_set

    assert store.get_setting("active_node_id") == str(old)          # the store still lags
    assert store.get_setting("pending_active_node_id") == str(new)  # ...but intent was recorded

    assert reapply_active_node(state).ok is True
    assert store.get_setting("active_node_id") == str(new)          # converged FORWARD
    assert store.get_setting("pending_active_node_id") == ""


def test_completed_apply_leaves_no_pending_marker(settings, stub_xray):
    settings.xray_bin = stub_xray
    store = _store(settings)
    nid = store.add_node(Node(id=None, name="n", address="1.2.3.3", port=443, uuid="u1",
                              sni="x", public_key="PK", short_id="ab"))
    state = _State(store, settings, sup=_CrashAfterReload())
    assert apply_node(store.get_node(nid), settings, state.supervisor, state.net,
                      store=store, xray_bin=stub_xray).ok is True
    assert store.get_setting("pending_active_node_id") == ""
    assert store.get_setting("active_node_id") == str(nid)


# --- 6. the watchdog backs off and does not flood the 40-entry event ring -----------------

def test_watchdog_backs_off_and_does_not_flood_the_event_ring():
    """A crash-looping xray used to be respawned every 20 s, each attempt writing an event —
    enough to erase every connect/failover/kill-switch record in about 13 minutes."""
    store = _store()
    conn_events.record(store, "connect", "the history that must survive", now=0)
    state = _State(store, sup=_FakeSup("error"))          # never comes up
    clock = {"t": 1000.0}
    loop = LivenessLoop(state, now=lambda: clock["t"])

    for _ in range(200):                                  # ~67 minutes of 20 s ticks
        loop._watchdog()
        clock["t"] += 20.0

    assert state.supervisor.started < 20                  # backed off, not once per tick
    events = conn_events.recent(store)
    assert any(e["kind"] == "connect" for e in events)    # history NOT flushed out
    restarts = [e for e in events if e["kind"] == "xray-restart"]
    assert 1 <= len(restarts) <= 3                        # coalesced, not one per attempt
    # the backoff must actually reach its ceiling rather than growing without bound
    assert loop._watchdog_backoff() == LivenessLoop.WATCHDOG_MAX_BACKOFF


def test_watchdog_restarts_immediately_after_a_long_healthy_run():
    store = _store()
    sup = _FakeSup("error", comes_up=True)
    state = _State(store, sup=sup)
    clock = {"t": 1000.0}
    loop = LivenessLoop(state, now=lambda: clock["t"])

    loop._watchdog()
    assert sup.started == 1
    sup._state = "error"                                  # crashes again, hours later
    clock["t"] += 4 * 3600
    loop._watchdog()
    assert sup.started == 2                               # a fresh episode, no stale backoff


def test_watchdog_reports_a_persistent_crash_loop_once():
    store = _store()
    state = _State(store, sup=_FakeSup("error"))
    clock = {"t": 1000.0}
    loop = LivenessLoop(state, now=lambda: clock["t"])
    for _ in range(400):
        loop._watchdog()
        clock["t"] += 20.0
    details = [e["detail"] for e in conn_events.recent(store) if e["kind"] == "xray-restart"]
    assert sum("keeps crashing" in d for d in details) == 1


# --- F4-12: a refresh must not revert a failover that landed while it ran -----------------

def test_subscription_refresh_does_not_revert_a_concurrent_failover():
    from pi_gw_panel.subs import service

    store = _store()
    old = store.add_node(Node(id=None, name="old", address="1.2.3.1", port=443, uuid="u1"))
    new = store.add_node(Node(id=None, name="new", address="1.2.3.2", port=443, uuid="u2"))
    # failover already moved the tunnel while the refresh transaction was committing
    store.set_setting("active_node_id", str(new))
    applied = []
    state = _State(store)
    state.settings = Settings()

    result = service._restart_active(state, old, {"active_changed": True})
    assert result is None and applied == []
    assert store.get_setting("active_node_id") == str(new)


def test_subscription_refresh_still_reapplies_when_the_active_node_held_still(monkeypatch):
    from pi_gw_panel.subs import service

    store = _store()
    nid = store.add_node(Node(id=None, name="n", address="1.2.3.1", port=443, uuid="u1"))
    store.set_setting("active_node_id", str(nid))
    applied = []
    monkeypatch.setattr(service, "apply_node",
                        lambda node, *_a, **_k: applied.append(node.id) or ApplyResult(ok=True))
    assert service._restart_active(_State(store), nid, {"active_changed": True}).ok is True
    assert applied == [nid]


# --- F7-7: nothing reaches the store once the loop has been stopped -----------------------

def test_monitor_refuses_probe_writes_once_stopped(settings):
    store = _store(settings)
    node_id = store.add_node(Node(id=None, name="a", address="1.2.3.1", port=443, uuid="u"))
    stop_event = threading.Event()
    stop_event.set()
    dialled = []
    monitor = HealthMonitor(_State(store, settings),
                            tcp_ping=lambda *_a: dialled.append(1) or (True, 1),
                            http_ping=lambda *_a: dialled.append(1) or (True, 1),
                            real_request=lambda *_a: (True, 200, 1, None),
                            now_iso=lambda: _ts(1000))
    monitor.run_once(stop_event)
    assert dialled == [] and store.get_health(node_id) is None
