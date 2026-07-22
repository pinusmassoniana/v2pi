from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.net_control.plan import NetResult


def _client(settings, stub_xray):
    settings.xray_bin = stub_xray
    return TestClient(create_app(settings, state=build_state(settings, net=DryRunBackend())))


def _login(c):
    c.post("/api/setup", json={"username": "admin", "password": "changeme"})
    return c.get("/api/csrf").json()["csrf"]


def _add_and_apply(c, tok):
    body = {"name": "n1", "address": "1.2.3.4", "port": 47000, "uuid": "u-1",
            "sni": "www.microsoft.com", "public_key": "PK", "short_id": "ab12"}
    nid = c.post("/api/nodes", json=body, headers={"X-CSRF-Token": tok}).json()["id"]
    c.post(f"/api/nodes/{nid}/apply", headers={"X-CSRF-Token": tok})
    return nid


def test_status_includes_xray_state(settings, stub_xray):
    c = _client(settings, stub_xray); tok = _login(c)
    assert c.get("/api/status").json()["xray_state"] == "stopped"
    _add_and_apply(c, tok)
    assert c.get("/api/status").json()["xray_state"] == "working"


def test_disconnect_clears_active_keeps_xray(settings, stub_xray):
    c = _client(settings, stub_xray); tok = _login(c)
    nid = _add_and_apply(c, tok)
    r = c.post(f"/api/nodes/{nid}/disconnect", headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    st = c.get("/api/status").json()
    assert st["active_node_id"] is None     # disconnected
    assert st["running"] is True            # xray left running


def test_disconnect_needs_csrf(settings, stub_xray):
    c = _client(settings, stub_xray); tok = _login(c)
    nid = _add_and_apply(c, tok)
    assert c.post(f"/api/nodes/{nid}/disconnect").status_code == 403


def test_xray_stop_then_start(settings, stub_xray):
    c = _client(settings, stub_xray); tok = _login(c)
    _add_and_apply(c, tok)
    c.post("/api/xray/stop", headers={"X-CSRF-Token": tok})
    assert c.get("/api/status").json()["running"] is False
    c.post("/api/xray/start", headers={"X-CSRF-Token": tok})
    assert c.get("/api/status").json()["running"] is True


def test_active_node_patch_and_delete_require_disconnect(settings, stub_xray):
    c = _client(settings, stub_xray); tok = _login(c)
    nid = _add_and_apply(c, tok)
    h = {"X-CSRF-Token": tok}
    assert c.patch(f"/api/nodes/{nid}", json={"name": "changed"}, headers=h).status_code == 409
    assert c.delete(f"/api/nodes/{nid}", headers=h).status_code == 409
    assert c.get(f"/api/nodes").json()[0]["name"] == "n1"


class _FailGuardNet(DryRunBackend):
    def apply_guard(self, plan):
        return NetResult(ok=False, error="guard failed")


def test_disconnect_does_not_persist_success_when_guard_fails(settings, stub_xray):
    settings.xray_bin = stub_xray
    state = build_state(settings, net=_FailGuardNet())
    c = TestClient(create_app(settings, state=state)); tok = _login(c)
    nid = _add_and_apply(c, tok)
    r = c.post(f"/api/nodes/{nid}/disconnect", headers={"X-CSRF-Token": tok})
    assert r.status_code == 502
    assert c.get("/api/status").json()["active_node_id"] == nid
