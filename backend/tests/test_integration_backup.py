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
    assert c.post("/api/restore", json=doc, headers=h).status_code == 200
    nodes = c.get("/api/nodes").json()
    assert len(nodes) == 1 and nodes[0]["id"] == nid and nodes[0]["name"] == "keep"
    assert c.get("/api/routing").json()["default_action"] == "direct"
    # session survived the restore (auth_* not touched)
    assert c.get("/api/csrf").status_code == 200
