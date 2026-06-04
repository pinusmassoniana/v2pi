"""Coverage for the Tuning-panel audit fixes/features (TC1/TC2, TB1, is_active, TN1/TN6-8)."""
from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.models import TuningProfile
from pi_gw_panel.xray_config.tuning import validate_profile


# --- TC2: structural validation ---
def test_validate_profile_catches_bad_values():
    assert validate_profile(TuningProfile(id=1, name="p", quic="nope"))[0] is False
    assert validate_profile(TuningProfile(id=1, name="p", frag_enabled=True, frag_packets="weird"))[0] is False
    assert validate_profile(TuningProfile(id=1, name="p", frag_enabled=True, frag_length="x"))[0] is False
    assert validate_profile(TuningProfile(id=1, name="p", doh_enabled=True, doh_url="ftp://x"))[0] is False
    assert validate_profile(TuningProfile(id=1, name="p", noise_enabled=True,
                                          noises=[{"type": "bogus"}]))[0] is False
    assert validate_profile(TuningProfile(id=1, name="p", frag_enabled=True))[0] is True


def _client(settings, stub_xray):
    settings.xray_bin = stub_xray
    return TestClient(create_app(settings, state=build_state(settings, net=DryRunBackend())))


def _login(c):
    c.post("/api/setup", json={"username": "admin", "password": "changeme"})
    return c.get("/api/csrf").json()["csrf"]


def _add_node(c, h):
    return c.post("/api/nodes", json={"name": "n", "address": "1.2.3.4", "port": 443, "uuid": "u",
                                      "sni": "s", "public_key": "PK", "short_id": "ab"},
                  headers=h).json()["id"]


def test_add_profile_rejects_bad(settings, stub_xray):
    c = _client(settings, stub_xray); h = {"X-CSRF-Token": _login(c)}
    assert c.post("/api/profiles", json={"name": "p", "quic": "nope"}, headers=h).status_code == 422


def test_noises_round_trip(settings, stub_xray):
    c = _client(settings, stub_xray); h = {"X-CSRF-Token": _login(c)}
    pid = c.post("/api/profiles", json={"name": "p", "noise_enabled": True,
                 "noises": [{"type": "rand", "packet": "50-150", "delay": "10-16"}]}, headers=h).json()["id"]
    p = next(x for x in c.get("/api/profiles").json() if x["id"] == pid)
    assert p["noise_enabled"] is True and p["noises"][0]["type"] == "rand"


def test_profile_node_count(settings, stub_xray):
    c = _client(settings, stub_xray); h = {"X-CSRF-Token": _login(c)}
    pid = c.post("/api/profiles", json={"name": "p"}, headers=h).json()["id"]
    nid = _add_node(c, h)
    c.patch(f"/api/nodes/{nid}", json={"tuning_profile_id": pid}, headers=h)
    p = next(x for x in c.get("/api/profiles").json() if x["id"] == pid)
    assert p["node_count"] == 1


def test_default_profile_marked_active_when_inherited(settings, stub_xray):
    c = _client(settings, stub_xray); h = {"X-CSRF-Token": _login(c)}
    nid = _add_node(c, h)
    assert c.post(f"/api/nodes/{nid}/apply", headers=h).status_code == 200   # node inherits default
    profs = c.get("/api/profiles").json()
    default = next(p for p in profs if p["is_default"])
    assert default["is_active"] is True                                       # even the default


def test_profile_validate_endpoint(settings, stub_xray):
    c = _client(settings, stub_xray); h = {"X-CSRF-Token": _login(c)}
    ok = c.post("/api/profiles/validate", json={"name": "p", "frag_enabled": True,
                "frag_packets": "tlshello"}, headers=h)
    assert ok.json()["ok"] is True
    bad = c.post("/api/profiles/validate", json={"name": "p", "frag_enabled": True,
                 "frag_packets": "weird"}, headers=h)
    assert bad.json()["ok"] is False


def test_profile_presets(settings, stub_xray):
    c = _client(settings, stub_xray); _login(c)
    names = [p["name"] for p in c.get("/api/profiles/presets").json()]
    assert "ru-hardened" in names


def test_apply_active(settings, stub_xray):
    c = _client(settings, stub_xray); h = {"X-CSRF-Token": _login(c)}
    pid = c.post("/api/profiles", json={"name": "hardened", "frag_enabled": True}, headers=h).json()["id"]
    assert c.post(f"/api/profiles/{pid}/apply-active", headers=h).status_code == 409   # no active node
    nid = _add_node(c, h)
    c.post(f"/api/nodes/{nid}/apply", headers=h)
    r = c.post(f"/api/profiles/{pid}/apply-active", headers=h)
    assert r.status_code == 200 and r.json()["node_id"] == nid
    node = next(n for n in c.get("/api/nodes").json() if n["id"] == nid)
    assert node["tuning_profile_id"] == pid
