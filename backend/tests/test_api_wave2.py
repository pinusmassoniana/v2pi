from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.models import NodeHealth


def _client(settings, stub_xray):
    settings.xray_bin = stub_xray
    app = create_app(settings, state=build_state(settings, net=DryRunBackend()))
    return TestClient(app)


def _login(c):
    c.post("/api/setup", json={"username": "admin", "password": "changeme"})
    return c.get("/api/csrf").json()["csrf"]


# --- Task 10: profiles ---

def test_profiles_crud_and_default(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    profs = c.get("/api/profiles").json()
    assert any(p["name"] == "default" and p["is_default"] for p in profs)   # migration seed

    assert c.post("/api/profiles", json={"name": "x"}).status_code == 403   # csrf required
    r = c.post("/api/profiles", json={"name": "frag", "frag_enabled": True, "quic": "drop"}, headers=h)
    assert r.status_code == 200
    pid = r.json()["id"]
    assert r.json()["frag_enabled"] is True and r.json()["quic"] == "drop"
    assert r.json()["is_default"] is False and r.json()["doh_enabled"] is True

    assert c.patch(f"/api/profiles/{pid}", json={"name": "frag2", "mux_enabled": True},
                   headers=h).json()["name"] == "frag2"

    rd = c.put("/api/profiles/default", json={"id": pid}, headers=h)
    assert rd.status_code == 200 and rd.json()["is_default"] is True
    assert next(p for p in c.get("/api/profiles").json() if p["id"] == pid)["is_default"] is True

    # move default away, then the old default-target is deletable
    seeded = next(p for p in c.get("/api/profiles").json() if p["name"] == "default")
    c.put("/api/profiles/default", json={"id": seeded["id"]}, headers=h)
    assert c.delete(f"/api/profiles/{pid}", headers=h).status_code == 200
    assert all(p["id"] != pid for p in c.get("/api/profiles").json())


def test_cannot_delete_default_profile(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    seeded = next(p for p in c.get("/api/profiles").json() if p["is_default"])
    assert c.delete(f"/api/profiles/{seeded['id']}", headers=h).status_code == 409


def test_node_tuning_profile_assign_and_clear(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid = c.post("/api/nodes", json={"name": "n", "address": "1.1.1.1", "port": 443, "uuid": "u"},
                 headers=h).json()["id"]
    pid = c.post("/api/profiles", json={"name": "p"}, headers=h).json()["id"]
    assert c.patch(f"/api/nodes/{nid}", json={"tuning_profile_id": pid},
                   headers=h).json()["tuning_profile_id"] == pid
    # explicit null clears the assignment (→ inherit default)
    assert c.patch(f"/api/nodes/{nid}", json={"tuning_profile_id": None},
                   headers=h).json()["tuning_profile_id"] is None


# --- Task 11: routing ---

def test_routing_get_put_and_preset(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    r = c.get("/api/routing").json()
    assert r["rules"] == [] and r["default_action"] == "proxy"

    assert c.put("/api/routing", json={"rules": [], "default_action": "direct"}).status_code == 403
    rp = c.put("/api/routing", json={
        "rules": [
            {"type": "geoip", "value": "ru", "action": "direct"},
            {"type": "domain", "value": "x.com", "action": "block"},
        ],
        "default_action": "direct"}, headers=h)
    assert rp.status_code == 200
    body = rp.json()
    assert [r["type"] for r in body["rules"]] == ["geoip", "domain"]
    assert [r["position"] for r in body["rules"]] == [0, 1]
    assert body["default_action"] == "direct"
    assert c.get("/api/routing").json()["default_action"] == "direct"      # persisted

    pre = c.post("/api/routing/preset/ru-direct", headers=h).json()
    assert ("geosite", "category-ru") in [(r["type"], r["value"]) for r in pre["rules"]]
    n1 = len(pre["rules"])
    pre2 = c.post("/api/routing/preset/ru-direct", headers=h).json()
    assert len(pre2["rules"]) == n1                                        # idempotent, no dupes


# --- Task 12: node-health + settings ---

def test_node_health_endpoint(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid = c.post("/api/nodes", json={"name": "n", "address": "1.1.1.1", "port": 443, "uuid": "u"},
                 headers=h).json()["id"]
    assert c.get("/api/node-health").json() == []                          # nothing probed yet
    # seed a row directly (the monitor doesn't tick during the test)
    c.app.state.app_state.store.upsert_health(NodeHealth(
        node_id=nid, last_tcp_ok=True, last_tcp_ms=10, last_real_ok=True,
        last_real_ms=20, egress_ip="9.9.9.9", checked_at="2026-06-03T00:00:00Z", fail_count=0))
    rows = c.get("/api/node-health").json()
    assert len(rows) == 1 and rows[0]["node_id"] == nid
    assert rows[0]["egress_ip"] == "9.9.9.9" and rows[0]["last_real_ok"] is True


def test_node_health_requires_auth(settings, stub_xray):
    c = _client(settings, stub_xray)
    assert c.get("/api/node-health").status_code == 401   # distinct from the open /api/health
