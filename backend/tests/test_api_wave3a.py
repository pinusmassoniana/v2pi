from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.net_control.dryrun import DryRunBackend


def _client(settings, stub_xray):
    settings.xray_bin = stub_xray
    return TestClient(create_app(settings, state=build_state(settings, net=DryRunBackend())))


def test_setup_creates_credential_and_authenticates(settings, stub_xray):
    c = _client(settings, stub_xray)
    assert c.get("/api/setup").json()["needs_setup"] is True
    # login before any credential exists fails
    assert c.post("/api/login", json={"username": "admin", "password": "x"}).status_code == 401
    r = c.post("/api/setup", json={"username": "admin", "password": "s3cret12"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert c.get("/api/setup").json()["needs_setup"] is False
    assert c.get("/api/csrf").json()["csrf"]                 # setup opened a session
    # re-setup is blocked once configured
    assert c.post("/api/setup", json={"username": "x", "password": "password1"}).status_code == 409
    # too-short password is rejected by validation
    assert c.post("/api/setup", json={"username": "a", "password": "short"}).status_code == 422


def test_login_with_created_credential(settings, stub_xray):
    c = _client(settings, stub_xray)
    c.post("/api/setup", json={"username": "admin", "password": "s3cret12"})
    c.post("/api/logout")
    assert c.get("/api/csrf").status_code == 401
    assert c.post("/api/login", json={"username": "admin", "password": "wrongpw1"}).status_code == 401
    assert c.post("/api/login", json={"username": "nope", "password": "s3cret12"}).status_code == 401
    assert c.post("/api/login", json={"username": "admin", "password": "s3cret12"}).status_code == 200
    assert c.get("/api/csrf").json()["csrf"]


def test_password_change(settings, stub_xray):
    c = _client(settings, stub_xray)
    c.post("/api/setup", json={"username": "admin", "password": "oldpass1"})
    h = {"X-CSRF-Token": c.get("/api/csrf").json()["csrf"]}
    # csrf required
    assert c.post("/api/password", json={"current_password": "oldpass1", "new_password": "newpass1"}).status_code == 403
    # wrong current rejected
    assert c.post("/api/password", json={"current_password": "nope", "new_password": "newpass1"},
                  headers=h).status_code == 403
    # correct current rotates the hash
    assert c.post("/api/password", json={"current_password": "oldpass1", "new_password": "newpass1"},
                  headers=h).status_code == 200
    c.post("/api/logout")
    assert c.post("/api/login", json={"username": "admin", "password": "oldpass1"}).status_code == 401
    assert c.post("/api/login", json={"username": "admin", "password": "newpass1"}).status_code == 200


def test_backup_and_restore_api(settings, stub_xray):
    c = _client(settings, stub_xray)
    c.post("/api/setup", json={"username": "admin", "password": "changeme"})
    h = {"X-CSRF-Token": c.get("/api/csrf").json()["csrf"]}
    c.post("/api/nodes", json={"name": "bk", "address": "5.5.5.5", "port": 443, "uuid": "ub"}, headers=h)
    doc = c.get("/api/backup").json()
    assert doc["schema_version"] == 2 and any(n["name"] == "bk" for n in doc["nodes"])
    assert c.post("/api/restore", json=doc).status_code == 403          # csrf required
    r = c.post("/api/restore", json=doc, headers=h)
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["restored"]["nodes"] == 1
    bad = {"schema_version": 999, "nodes": [], "subscriptions": [], "profiles": [],
           "routing": {"rules": [], "default_action": "proxy"}, "settings": {}}
    assert c.post("/api/restore", json=bad, headers=h).status_code == 400


def test_logs_endpoint(settings, stub_xray):
    import os
    c = _client(settings, stub_xray)
    c.post("/api/setup", json={"username": "admin", "password": "changeme"})
    p = c.app.state.app_state.settings.xray_error_log
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write("err line 1\nerr line 2\n")
    r = c.get("/api/logs?source=xray-error&lines=1").json()
    assert r["source"] == "xray-error" and r["lines"] == ["err line 2"]
    assert c.get("/api/logs?source=bogus").status_code == 400      # unknown source
