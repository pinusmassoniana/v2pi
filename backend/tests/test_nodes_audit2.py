"""Backend coverage for the 2026-06-04 Nodes-panel audit (v1.6.1): per-node note (N-C)."""
from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.models import Node
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore


def _client(settings, stub_xray):
    settings.xray_bin = stub_xray
    c = TestClient(create_app(settings, state=build_state(settings, net=DryRunBackend())))
    c.post("/api/setup", json={"username": "admin", "password": "changeme"})
    return c, c.get("/api/csrf").json()["csrf"]


def test_note_round_trips_through_api(settings, stub_xray):
    c, tok = _client(settings, stub_xray)
    h = {"X-CSRF-Token": tok}
    created = c.post("/api/nodes", json={"name": "n1", "address": "1.2.3.4", "port": 443,
                                         "uuid": "u-1", "note": "paid until July"}, headers=h).json()
    assert created["note"] == "paid until July"
    nid = created["id"]
    # patch only the note → persists, other fields untouched
    patched = c.patch(f"/api/nodes/{nid}", json={"note": "fast EU node"}, headers=h).json()
    assert patched["note"] == "fast EU node" and patched["address"] == "1.2.3.4"
    # clearing the note is allowed
    assert c.patch(f"/api/nodes/{nid}", json={"note": ""}, headers=h).json()["note"] == ""
    # default when omitted
    plain = c.post("/api/nodes", json={"name": "n2", "address": "5.6.7.8", "port": 443,
                                       "uuid": "u-2"}, headers=h).json()
    assert plain["note"] == ""


def test_note_persisted_in_store(settings):
    conn = connect(settings.db_path, check_same_thread=False)
    init_schema(conn)
    store = NodeStore(conn)
    nid = store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u",
                              note="seed note"))
    assert store.get_node(nid).note == "seed note"
    n = store.get_node(nid)
    n.note = "edited"
    store.update_node(n)
    assert store.get_node(nid).note == "edited"
