from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.net_control.dryrun import DryRunBackend


def test_backup_wipe_restore_roundtrip_via_api(settings, stub_xray):
    settings.xray_bin = stub_xray
    c = TestClient(create_app(settings, state=build_state(settings, net=DryRunBackend())))
    c.post("/api/setup", json={"username": "admin", "password": "changeme"})
    h = {"X-CSRF-Token": c.get("/api/csrf").json()["csrf"]}

    nid = c.post("/api/nodes", json={"name": "keep", "address": "7.7.7.7", "port": 443, "uuid": "uk"},
                 headers=h).json()["id"]
    c.put("/api/routing", json={"rules": [{"type": "geoip", "value": "ru", "action": "direct"}],
                                "default_action": "direct"}, headers=h)
    doc = c.get("/api/backup").json()

    # wipe
    c.delete(f"/api/nodes/{nid}", headers=h)
    assert c.get("/api/nodes").json() == []

    # restore brings the node back (same id) + the routing default
    restored = c.post("/api/restore", json=doc, headers=h)
    assert restored.status_code == 200
    assert restored.json()["runtime"] == "disconnected"
    nodes = c.get("/api/nodes").json()
    assert len(nodes) == 1 and nodes[0]["id"] == nid and nodes[0]["name"] == "keep"
    assert c.get("/api/routing").json()["default_action"] == "direct"
    # session survived the restore (auth_* not touched)
    assert c.get("/api/csrf").status_code == 200


def test_restore_stops_live_tunnel_clears_selection_and_keeps_guard(settings, stub_xray):
    settings.xray_bin = stub_xray
    state = build_state(settings, net=DryRunBackend())
    c = TestClient(create_app(settings, state=state))
    c.post("/api/setup", json={"username": "admin", "password": "changeme"})
    h = {"X-CSRF-Token": c.get("/api/csrf").json()["csrf"]}
    node_id = c.post(
        "/api/nodes", json={"name": "live", "address": "7.7.7.7", "port": 443, "uuid": "u"},
        headers=h).json()["id"]
    assert c.post(f"/api/nodes/{node_id}/apply", headers=h).status_code == 200
    document = c.get("/api/backup").json()
    response = c.post("/api/restore", json=document, headers=h)
    assert response.status_code == 200 and response.json()["runtime"] == "disconnected"
    status = c.get("/api/status").json()
    assert status["running"] is False and status["active_node_id"] is None
    assert state.store.get_setting("prev_active_node_id") == ""
    assert "tproxy ip to" not in state.net.applied[-1] and " drop" in state.net.applied[-1]


def test_restore_reconfigures_stats_client_to_restored_port(settings, stub_xray):
    settings.xray_bin = stub_xray
    state = build_state(settings, net=DryRunBackend())
    c = TestClient(create_app(settings, state=state))
    c.post("/api/setup", json={"username": "admin", "password": "changeme"})
    h = {"X-CSRF-Token": c.get("/api/csrf").json()["csrf"]}
    document = c.get("/api/backup").json()
    document["settings"]["stats_api_port"] = "10086"

    response = c.post("/api/restore", json=document, headers=h)

    assert response.status_code == 200
    assert state.stats_client.status()["address"] == "127.0.0.1:10086"
