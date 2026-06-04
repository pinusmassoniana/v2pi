"""Coverage for the Nodes-panel audit fixes/features (NC1–NC3, NR2, NN3/NN4/NN10)."""
from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.models import Node, Subscription, NodeHealth
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.health.monitor import HealthMonitor
from pi_gw_panel.health.failover import decide


def _store(settings):
    conn = connect(settings.db_path, check_same_thread=False)
    init_schema(conn)
    return NodeStore(conn)


class _State:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings


def _mon_state(settings):
    return _State(_store(settings), settings)


# --- NC1: no real-probe / no spurious failover when tunneled_fetch is off ---
def test_monitor_skips_real_probe_when_tunnel_off(settings):
    st = _mon_state(settings)
    a = st.store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u"))
    st.store.set_setting("active_node_id", str(a))
    st.store.set_setting("tunneled_fetch", "0")
    called = []
    mon = HealthMonitor(st, tcp_ping=lambda *_: (True, 5), http_ping=lambda *_: (True, 9),
                        real_request=lambda *a, **k: called.append(1) or (False, None, None, None),
                        now_iso=lambda: "t")
    mon.run_once()
    assert called == []                              # real probe skipped
    assert st.store.get_health(a).fail_count == 0    # → no failover pressure


# --- NC2: monitor preserves a prior per-node real/egress for non-active nodes ---
def test_monitor_preserves_real_egress_for_non_active(settings):
    st = _mon_state(settings)
    a = st.store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="ua"))
    b = st.store.add_node(Node(id=None, name="b", address="2.2.2.2", port=443, uuid="ub"))
    st.store.set_setting("active_node_id", str(a))
    st.store.upsert_health(NodeHealth(node_id=b, last_real_ok=True, last_real_ms=42, egress_ip="9.9.9.9"))
    mon = HealthMonitor(st, tcp_ping=lambda *_: (True, 5), http_ping=lambda *_: (True, 9),
                        real_request=lambda *_: (True, 200, 1, "x"), now_iso=lambda: "t")
    mon.run_once()
    hb = st.store.get_health(b)
    assert hb.last_real_ok is True and hb.last_real_ms == 42 and hb.egress_ip == "9.9.9.9"


# --- NC3: failover prefers the healthiest alive node and skips stale ---
def _h(node_id, tcp=False, http=False, fail=0):
    return NodeHealth(node_id=node_id, last_tcp_ok=tcp, last_http_ok=http, fail_count=fail)


def test_failover_prefers_healthiest_and_skips_stale():
    nodes = [Node(id=1, name="a", address="1.1.1.1", port=443, uuid="u1"),
             Node(id=2, name="b", address="2.2.2.2", port=443, uuid="u2", stale=True),
             Node(id=3, name="c", address="3.3.3.3", port=443, uuid="u3")]
    health = {1: _h(1, tcp=True, fail=3), 2: _h(2, tcp=True), 3: _h(3, http=True)}
    # active=1 over hysteresis → pick 3 (http beats tcp), and stale 2 is skipped
    assert decide(health, nodes, 1, 3, 120, 1000, None) == 3


# --- NR2: scoped probe node selection ---
def test_scoped_nodes(settings):
    from pi_gw_panel.api.routes import _scoped_nodes
    s = _store(settings)
    sid = s.add_subscription(Subscription(id=None, name="x", url="u"))
    n_sub = s.add_node(Node(id=None, name="s", address="1.1.1.1", port=443, uuid="a", subscription_id=sid))
    n_man = s.add_node(Node(id=None, name="m", address="2.2.2.2", port=443, uuid="b"))
    assert {n.id for n in _scoped_nodes(s, "servers")} == {n_man}
    assert {n.id for n in _scoped_nodes(s, str(sid))} == {n_sub}
    assert len(_scoped_nodes(s, None)) == 2


# --- NN3: detach to manual ---
def test_detach_nodes(settings):
    s = _store(settings)
    sid = s.add_subscription(Subscription(id=None, name="x", url="u"))
    n = s.add_node(Node(id=None, name="n", address="1.1.1.1", port=443, uuid="a", subscription_id=sid))
    s.detach_nodes([n])
    assert s.get_node(n).subscription_id is None


# --- NN4: latency history caps and survives a health upsert ---
def test_record_latency_caps_and_survives_upsert(settings):
    s = _store(settings)
    nid = s.add_node(Node(id=None, name="n", address="1.1.1.1", port=443, uuid="u"))
    s.upsert_health(NodeHealth(node_id=nid, last_tcp_ok=True))
    for i in range(25):
        s.record_latency(nid, i, cap=20)
    assert s.get_health(nid).lat_history == list(range(5, 25))
    s.upsert_health(NodeHealth(node_id=nid, last_tcp_ok=False))   # a fresh sweep
    assert s.get_health(nid).lat_history == list(range(5, 25))    # history preserved


# --- NN10: pre-flight validate endpoint ---
def _client(settings, stub_xray):
    settings.xray_bin = stub_xray
    return TestClient(create_app(settings, state=__import__("pi_gw_panel.state", fromlist=["build_state"])
                                 .build_state(settings, net=DryRunBackend())))


def _login(c):
    c.post("/api/setup", json={"username": "admin", "password": "changeme"})
    return c.get("/api/csrf").json()["csrf"]


def test_validate_node_ok(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    r = c.post("/api/nodes/validate",
               json={"name": "x", "address": "a", "port": 443, "uuid": "u", "public_key": "PK"}, headers=h)
    assert r.status_code == 200 and r.json()["ok"] is True


def test_detach_endpoint(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    sid = c.post("/api/subs", json={"name": "s", "url": "https://h/x"}, headers=h).json()["id"]
    nid = c.post("/api/nodes", json={"name": "n", "address": "1.1.1.1", "port": 443, "uuid": "u"},
                 headers=h).json()["id"]
    # manually attach via a refresh would be heavy; just detach an already-manual node is a no-op,
    # so attach by importing into the sub is out of scope here — assert the endpoint wiring works.
    assert c.post("/api/nodes/detach", json={"ids": [nid]}, headers=h).status_code == 200
