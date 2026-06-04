from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.models import Node, NodeHealth
from pi_gw_panel.health.snapshot import active_health
from pi_gw_panel.stats.sampler import TrafficSampler
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore


def _store():
    conn = connect(":memory:")
    init_schema(conn)
    return NodeStore(conn)


def test_active_health_snapshot_and_none():
    store = _store()
    assert active_health(store) is None                       # no active node
    nid = store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u"))
    store.set_setting("active_node_id", str(nid))
    assert active_health(store) is None                       # active, but no health row yet
    store.upsert_health(NodeHealth(node_id=nid, last_real_ok=True, last_real_ms=21,
                                   egress_ip="8.8.8.8", checked_at="2026-06-04T00:00:00+00:00"))
    assert active_health(store) == {"node_id": nid, "real_ok": True, "latency_ms": 21,
                                    "egress_ip": "8.8.8.8", "egress_ip6": None,
                                    "checked_at": "2026-06-04T00:00:00+00:00",
                                    "lat_history": []}   # B: sparkline source, empty until probed


def test_sampler_totals_track_cumulative_bytes():
    counters = {"outbound>>>proxy>>>traffic>>>uplink": 1000,
                "outbound>>>proxy>>>traffic>>>downlink": 4000,
                "outbound>>>direct>>>traffic>>>uplink": 7}
    s = TrafficSampler(query_fn=lambda: counters, clock=lambda: 1.0)
    s.sample()
    assert s.totals["proxy"] == {"up": 1000, "down": 4000}
    assert s.totals["direct"] == {"up": 7, "down": 0}


def test_active_since_set_on_apply_and_cleared_on_disconnect(settings, stub_xray):
    settings.xray_bin = stub_xray
    state = build_state(settings, net=DryRunBackend())
    c = TestClient(create_app(settings, state=state))
    c.post("/api/setup", json={"username": "admin", "password": "changeme"})
    tok = c.get("/api/csrf").json()["csrf"]
    h = {"X-CSRF-Token": tok}
    nid = c.post("/api/nodes", json={"name": "n1", "address": "1.2.3.4", "port": 443, "uuid": "u-1"},
                 headers=h).json()["id"]
    assert c.post(f"/api/nodes/{nid}/apply", headers=h).status_code == 200
    since = c.get("/api/status").json()["active_since"]
    assert isinstance(since, int) and since > 0               # uptime anchor set on apply (P3)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    assert c.get("/api/status").json()["active_since"] is None  # cleared on disconnect
