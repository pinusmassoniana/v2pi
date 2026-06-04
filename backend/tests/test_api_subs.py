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
    assert s["health_enabled"] is True and s["health_interval"] == 1800   # health defaults (30 min)
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
            '[{"name":"a","address":"9.9.9.9","port":443,"uuid":"u9"}]', "direct", {}))
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


def test_sub_interval_clamped_and_lifecycle_fields(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    r = c.post("/api/subs", json={"name": "s", "url": "https://h/x", "interval_sec": 5}, headers=h).json()
    assert r["interval_sec"] == 60                  # R3: floored from 5 → 60
    assert r["enabled"] is True and r["last_error"] is None and r["default_profile_id"] is None
    sid = r["id"]
    assert c.patch(f"/api/subs/{sid}", json={"interval_sec": 0}, headers=h).json()["interval_sec"] == 0
    assert c.patch(f"/api/subs/{sid}", json={"enabled": False}, headers=h).json()["enabled"] is False


def test_manual_xhttp_node_built_as_xhttp(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    r = c.post("/api/nodes", json={"name": "x", "address": "a", "port": 443, "uuid": "u",
                                   "transport": "xhttp", "path": "/p"}, headers=h).json()
    assert r["transport"] == "xhttp" and r["network"] == "xhttp"   # C3: not silently tcp
    assert r["security"] == "tls" and r["path"] == "/p"            # no key → tls


def test_reorder_nodes(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    a = c.post("/api/nodes", json={"name": "a", "address": "1.1.1.1", "port": 443, "uuid": "u1"},
               headers=h).json()["id"]
    b = c.post("/api/nodes", json={"name": "b", "address": "2.2.2.2", "port": 443, "uuid": "u2"},
               headers=h).json()["id"]
    assert c.post("/api/nodes/reorder", json={"ids": [b, a]}, headers=h).status_code == 200
    assert [n["id"] for n in c.get("/api/nodes").json()] == [b, a]


def test_preview_nodes_dry_run(settings, stub_xray, monkeypatch):
    import pi_gw_panel.api.routes as routes
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    monkeypatch.setattr(routes, "fetch", lambda url, inj, tok, *, proxy: (
        '[{"name": "a", "address": "9.9.9.9", "port": 443, "uuid": "u"}]', "direct", {}))
    r = c.post("/api/subs/preview-nodes", json={"url": "https://h/x"}, headers=h)
    assert r.status_code == 200
    assert r.json()["format"] == "json" and r.json()["count"] == 1
    assert r.json()["nodes"][0]["address"] == "9.9.9.9"


def test_connect_best_404_when_empty(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    assert c.post("/api/connect-best", json={"subscription_id": None}, headers=h).status_code == 404


def test_import_nodes_from_pasted_json(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    text = '[{"name":"i1","address":"5.5.5.5","port":443,"uuid":"u"}]'
    r = c.post("/api/nodes/import", json={"text": text}, headers=h)
    assert r.status_code == 200 and r.json()["added"] == 1 and r.json()["format"] == "json"
    nodes = c.get("/api/nodes").json()
    assert any(n["address"] == "5.5.5.5" and n["subscription_id"] is None for n in nodes)
    # re-import is idempotent (skipped by identity)
    assert c.post("/api/nodes/import", json={"text": text}, headers=h).json()["added"] == 0
