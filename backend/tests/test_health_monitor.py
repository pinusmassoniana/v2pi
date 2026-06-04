import asyncio
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.models import Node
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
                       real_request=lambda *_: (True, 200, 9, "x"),
                       after_tick=lambda: ticks.append(1))
        mon.start()
        await asyncio.sleep(0.05)
        await mon.stop()

    asyncio.run(drive())
    assert ticks  # after_tick ran at least once before cancellation
