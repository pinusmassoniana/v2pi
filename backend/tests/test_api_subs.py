from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.subs import service


def _client(settings, stub_xray):
    settings.xray_bin = stub_xray
    app = create_app(settings, state=build_state(settings, net=DryRunBackend()))
    return TestClient(app)


def _login(c):
    c.post("/api/setup", json={"username": "admin", "password": "changeme"})
    return c.get("/api/csrf").json()["csrf"]


def test_subscription_crud(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    assert c.post("/api/subs", json={"name": "s", "url": "https://h/x"}).status_code == 403  # no csrf
    r = c.post("/api/subs", json={"name": "s", "url": "https://h/x", "interval_sec": 3600}, headers=h)
    assert r.status_code == 200
    sid = r.json()["id"]
    assert r.json()["injection"]["headers"]["x-hwid"] == "{machine_id}"   # default injection
    assert r.json()["node_count"] == 0
    assert [s["id"] for s in c.get("/api/subs").json()] == [sid]
    assert c.patch(f"/api/subs/{sid}", json={"name": "renamed"}, headers=h).json()["name"] == "renamed"
    assert c.delete(f"/api/subs/{sid}", headers=h).status_code == 200
    assert c.get("/api/subs").json() == []


def test_node_patch_delete(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid = c.post("/api/nodes", json={"name": "n", "address": "1.1.1.1", "port": 443, "uuid": "u"},
                 headers=h).json()["id"]
    r = c.patch(f"/api/nodes/{nid}", json={"transport": "xhttp", "name": "n2"}, headers=h)
    assert r.status_code == 200 and r.json()["transport"] == "xhttp" and r.json()["name"] == "n2"
    assert c.delete(f"/api/nodes/{nid}", headers=h).status_code == 200
    assert c.get("/api/nodes").json() == []


def test_settings_get_put(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    s = c.get("/api/settings").json()
    assert s["tunneled_fetch"] is True                                  # default kept
    assert s["health_enabled"] is True and s["health_interval"] == 30   # health defaults
    assert s["failover_cooldown"] == 120 and s["routing_default_action"] == "proxy"
    assert s["stats_enabled"] is True and s["stats_api_port"] == 10085 and s["traffic_sample_ms"] == 1000
    assert "frag_enabled" not in s                                      # tuning fields → profiles
    r = c.put("/api/settings", json={"health_interval": 15, "failover_enabled": False,
                                     "routing_default_action": "direct", "stats_enabled": False}, headers=h)
    assert r.json()["health_interval"] == 15 and r.json()["failover_enabled"] is False
    assert r.json()["stats_enabled"] is False
    assert c.get("/api/settings").json()["routing_default_action"] == "direct"   # persisted


def test_subs_refresh_creates_nodes(settings, stub_xray, monkeypatch):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    sid = c.post("/api/subs", json={"name": "s", "url": "https://h/x"}, headers=h).json()["id"]
    monkeypatch.setattr(
        service, "fetch",
        lambda url, inj, tok, *, proxy: (
            '[{"name":"a","address":"9.9.9.9","port":443,"uuid":"u9"}]', "direct"))
    r = c.post(f"/api/subs/{sid}/refresh", headers=h)
    assert r.status_code == 200 and r.json()["added"] == 1 and r.json()["path"] == "direct"
    assert any(n["address"] == "9.9.9.9" for n in c.get("/api/nodes").json())


def test_subs_preview_no_network(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    r = c.post("/api/subs/preview",
               json={"url": "https://h/x", "injection": {"headers": {"x-hwid": "{machine_id}"}}}, headers=h)
    assert r.status_code == 200
    assert r.json()["method"] == "GET" and r.json()["url"] == "https://h/x"
    assert r.json()["headers"]["x-hwid"]   # token substituted to a non-empty machine id
