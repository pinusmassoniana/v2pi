from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.models import Node, NodeHealth
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.controller import ApplyResult
from pi_gw_panel.health import failover
from pi_gw_panel.health.failover import decide


def _nodes(*ids):
    return [Node(id=i, name=f"n{i}", address=f"{i}.{i}.{i}.{i}", port=443, uuid=f"u{i}")
            for i in ids]


def _h(node_id, tcp_ok=True, fail_count=0):
    return NodeHealth(node_id=node_id, last_tcp_ok=tcp_ok, fail_count=fail_count)


# --- decide (pure) ---------------------------------------------------------

def test_decide_no_failover_below_hysteresis():
    health = {1: _h(1, fail_count=2), 2: _h(2)}
    assert decide(health, _nodes(1, 2), 1, 3, 120, 1000, None) is None


def test_decide_picks_first_tcp_alive_other_in_node_order():
    health = {1: _h(1, fail_count=3), 2: _h(2, tcp_ok=False), 3: _h(3, tcp_ok=True)}
    assert decide(health, _nodes(1, 2, 3), 1, 3, 120, 1000, None) == 3   # skips dead node 2


def test_decide_none_when_no_alive_candidate():
    health = {1: _h(1, fail_count=5), 2: _h(2, tcp_ok=False)}
    assert decide(health, _nodes(1, 2), 1, 3, 120, 1000, None) is None


def test_decide_respects_cooldown():
    health = {1: _h(1, fail_count=3), 2: _h(2)}
    assert decide(health, _nodes(1, 2), 1, 3, 120, 1000, last_failover_at=950) is None  # 50<120
    assert decide(health, _nodes(1, 2), 1, 3, 120, 1100, last_failover_at=950) == 2      # 150>120


def test_decide_none_when_no_active_or_missing_health():
    assert decide({}, _nodes(1, 2), None, 3, 120, 1, None) is None
    assert decide({}, _nodes(1, 2), 1, 3, 120, 1, None) is None       # active has no health row


# --- run (store-backed, stubbed apply) -------------------------------------

class _State:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.supervisor, self.net = None, None


def _state(settings):
    conn = connect(settings.db_path, check_same_thread=False)
    init_schema(conn)
    return _State(NodeStore(conn), settings)


def test_run_switches_active_to_candidate(settings):
    st = _state(settings)
    a = st.store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u1"))
    b = st.store.add_node(Node(id=None, name="b", address="2.2.2.2", port=443, uuid="u2"))
    st.store.set_setting("active_node_id", str(a))
    st.store.upsert_health(NodeHealth(node_id=a, last_tcp_ok=True, fail_count=3))
    st.store.upsert_health(NodeHealth(node_id=b, last_tcp_ok=True, fail_count=0))
    applied = []

    def fake_apply(node, s, sup, net, store=None, xray_bin=None):
        applied.append(node.id)
        store.set_setting("active_node_id", str(node.id))
        return ApplyResult(ok=True)

    res = failover.run(st, now=1000.0, apply_fn=fake_apply)
    assert res == b and applied == [b]
    assert st.store.get_setting("active_node_id") == str(b)
    assert st.store.get_setting("last_failover_at") == "1000.0"


def test_run_noop_when_disabled(settings):
    st = _state(settings)
    a = st.store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u1"))
    st.store.add_node(Node(id=None, name="b", address="2.2.2.2", port=443, uuid="u2"))
    st.store.set_setting("active_node_id", str(a))
    st.store.upsert_health(NodeHealth(node_id=a, last_tcp_ok=True, fail_count=9))
    st.store.set_setting("failover_enabled", "0")
    called = []
    assert failover.run(st, now=1.0, apply_fn=lambda *a, **k: called.append(1)) is None
    assert called == []


def test_run_noop_and_no_timestamp_when_apply_fails(settings):
    st = _state(settings)
    a = st.store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u1"))
    b = st.store.add_node(Node(id=None, name="b", address="2.2.2.2", port=443, uuid="u2"))
    st.store.set_setting("active_node_id", str(a))
    st.store.upsert_health(NodeHealth(node_id=a, last_tcp_ok=True, fail_count=3))
    st.store.upsert_health(NodeHealth(node_id=b, last_tcp_ok=True, fail_count=0))
    res = failover.run(st, now=1.0, apply_fn=lambda *a, **k: ApplyResult(ok=False, error="boom"))
    assert res is None
    assert st.store.get_setting("last_failover_at") is None
