import asyncio
import threading
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.models import Node, NodeHealth
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.health.monitor import HealthMonitor


class _State:
    def __init__(self, store, settings):
        self.store = store
        self.settings = settings


def _state(settings):
    # check_same_thread=False so the loop's worker thread can use the conn (prod wiring)
    conn = connect(settings.db_path, check_same_thread=False)
    init_schema(conn)
    return _State(NodeStore(conn), settings)


def _monitor(st, **kw):
    return HealthMonitor(st, now_iso=lambda: "2026-06-03T00:00:00Z", **kw)


def test_run_once_writes_tcp_for_all_and_real_for_active(settings):
    st = _state(settings)
    a = st.store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u1"))
    b = st.store.add_node(Node(id=None, name="b", address="2.2.2.2", port=443, uuid="u2"))
    st.store.set_setting("active_node_id", str(a))
    mon = _monitor(st, tcp_ping=lambda addr, port: (True, 12),
                   http_ping=lambda addr, port, sni: (True, 50),
                   real_request=lambda proxy, url: (True, 200, 34, "203.0.113.5"))
    mon.run_once()
    ha = st.store.get_health(a)
    assert ha.last_tcp_ok is True and ha.last_tcp_ms == 12
    assert ha.last_http_ok is True and ha.last_http_ms == 50            # direct HTTPS probe
    assert ha.last_real_ok is True and ha.last_real_ms == 34 and ha.egress_ip == "203.0.113.5"
    assert ha.fail_count == 0 and ha.checked_at == "2026-06-03T00:00:00Z"
    # non-active node gets TCP + HTTP, but no through-tunnel real
    hb = st.store.get_health(b)
    assert hb.last_tcp_ok is True and hb.last_http_ok is True and hb.last_http_ms == 50
    assert hb.last_real_ok is None and hb.egress_ip is None


def test_consecutive_real_failures_increment_then_reset(settings):
    st = _state(settings)
    a = st.store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u1"))
    st.store.set_setting("active_node_id", str(a))
    fail = _monitor(st, tcp_ping=lambda *_: (True, 5), http_ping=lambda *_: (True, 1),
                    real_request=lambda *_: (False, None, None, None))
    fail.run_once()
    assert st.store.get_health(a).fail_count == 1
    fail.run_once()
    assert st.store.get_health(a).fail_count == 2
    ok = _monitor(st, tcp_ping=lambda *_: (True, 5), http_ping=lambda *_: (True, 1),
                  real_request=lambda *_: (True, 200, 9, "1.2.3.4"))
    ok.run_once()
    assert st.store.get_health(a).fail_count == 0


def test_atomic_partial_health_updates_preserve_both_probe_owners(settings):
    st = _state(settings)
    node_id = st.store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u1"))
    st.store.upsert_health(NodeHealth(node_id=node_id, fail_count=4))
    barrier = threading.Barrier(3)

    def direct_update():
        barrier.wait()
        st.store.update_health_direct(
            node_id, tcp_ok=True, tcp_ms=11, http_ok=True, http_ms=22,
            checked_at="2026-07-22T10:00:00+00:00",
        )

    def real_update():
        barrier.wait()
        st.store.update_health_real(
            node_id, real_ok=False, real_ms=33, egress_ip="203.0.113.9",
            egress_ip6=None, checked_at="2026-07-22T10:00:01+00:00",
        )

    threads = [threading.Thread(target=direct_update), threading.Thread(target=real_update)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    health = st.store.get_health(node_id)
    assert (health.last_tcp_ok, health.last_tcp_ms, health.last_http_ok, health.last_http_ms) == (
        True, 11, True, 22,
    )
    assert (health.last_real_ok, health.last_real_ms, health.egress_ip, health.fail_count) == (
        False, 33, "203.0.113.9", 5,
    )


def test_stale_real_result_cannot_overwrite_newer_success(settings):
    st = _state(settings)
    node_id = st.store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u1"))
    st.store.update_health_real(
        node_id, real_ok=True, real_ms=10, egress_ip="203.0.113.10",
        egress_ip6=None, checked_at="2026-07-22T10:00:02+00:00",
    )

    st.store.update_health_real(
        node_id, real_ok=False, real_ms=99, egress_ip=None,
        egress_ip6=None, checked_at="2026-07-22T10:00:01+00:00",
    )

    health = st.store.get_health(node_id)
    assert (health.last_real_ok, health.last_real_ms, health.egress_ip, health.fail_count) == (
        True, 10, "203.0.113.10", 0,
    )


def test_monitor_rechecks_active_before_real_probe_and_keeps_direct_result(settings):
    st = _state(settings)
    first = st.store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u1"))
    second = st.store.add_node(Node(id=None, name="b", address="2.2.2.2", port=443, uuid="u2"))
    st.store.set_setting("active_node_id", str(first))
    real_calls = []

    def http_ping(address, *_args):
        if address == "1.1.1.1":
            st.store.set_setting("active_node_id", str(second))
        return True, 22

    mon = _monitor(
        st,
        tcp_ping=lambda *_: (True, 11),
        http_ping=http_ping,
        real_request=lambda *_: real_calls.append(1) or (False, None, None, None),
    )
    mon.run_once()

    assert real_calls == []
    health = st.store.get_health(first)
    assert health.last_tcp_ok is True and health.last_http_ok is True
    assert health.last_real_ok is None and health.fail_count == 0


def test_monitor_discards_real_result_if_active_switches_during_probe(settings):
    st = _state(settings)
    first = st.store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u1"))
    second = st.store.add_node(Node(id=None, name="b", address="2.2.2.2", port=443, uuid="u2"))
    st.store.set_setting("active_node_id", str(first))
    st.store.upsert_health(NodeHealth(node_id=first, last_real_ok=True, fail_count=4))

    def switch_during_probe(*_args):
        st.store.set_setting("active_node_id", str(second))
        return False, None, None, None

    mon = _monitor(
        st,
        tcp_ping=lambda *_: (True, 11),
        http_ping=lambda *_: (True, 22),
        real_request=switch_during_probe,
    )
    mon.run_once()

    health = st.store.get_health(first)
    assert health.last_tcp_ok is True and health.last_http_ok is True
    assert health.last_real_ok is True and health.fail_count == 4


def test_active_direct_probe_does_not_refresh_old_real_health(settings):
    st = _state(settings)
    node_id = st.store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u1"))
    st.store.set_setting("active_node_id", str(node_id))
    st.store.set_setting("tunneled_fetch", "0")
    old_real_at = "2026-05-22T09:00:00+00:00"
    st.store.upsert_health(NodeHealth(
        node_id=node_id, last_real_ok=True, checked_at=old_real_at,
    ))

    _monitor(
        st, tcp_ping=lambda *_: (True, 11), http_ping=lambda *_: (True, 22),
    ).run_once()

    health = st.store.get_health(node_id)
    assert health.last_tcp_ok is True and health.last_http_ok is True
    assert health.checked_at == old_real_at


def test_disabled_monitor_is_noop(settings):
    st = _state(settings)
    a = st.store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u1"))
    st.store.set_setting("health_enabled", "0")
    mon = _monitor(st, tcp_ping=lambda *_: (True, 5), http_ping=lambda *_: (True, 1),
                   real_request=lambda *_: (True, 200, 9, "x"))
    mon.run_once()
    assert st.store.get_health(a) is None


def test_after_tick_called_and_loop_cancellable(settings):
    st = _state(settings)
    st.store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u1"))
    ticks = []

    async def drive():
        mon = _monitor(st, tick_sec=0.001, tcp_ping=lambda *_: (True, 5),
                       http_ping=lambda *_: (True, 6),
                       real_request=lambda *_: (True, 200, 9, "x"),
                       after_tick=lambda: ticks.append(1))
        mon.start()
        await asyncio.sleep(0.05)
        await mon.stop()

    asyncio.run(drive())
    assert ticks  # after_tick ran at least once before cancellation


def test_stop_waits_for_current_sweep_and_blocks_late_health_writes(settings):
    st = _state(settings)
    node_id = st.store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u1"))
    started = threading.Event()
    release = threading.Event()

    def slow_tcp(*_args):
        started.set()
        release.wait(timeout=1)
        return True, 11

    async def drive():
        mon = _monitor(
            st,
            tick_sec=60,
            tcp_ping=slow_tcp,
            http_ping=lambda *_: (True, 22),
            real_request=lambda *_: (True, 200, 33, "203.0.113.9"),
        )
        mon.start()
        assert await asyncio.to_thread(started.wait, 1)
        stopping = asyncio.create_task(mon.stop())
        await asyncio.sleep(0.02)
        returned_before_probe = stopping.done()
        release.set()
        await asyncio.wait_for(stopping, timeout=1)
        await asyncio.sleep(0.02)
        return returned_before_probe

    assert asyncio.run(drive()) is False
    assert st.store.get_health(node_id) is None
