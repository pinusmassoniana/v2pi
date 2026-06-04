"""Coverage for the Settings-panel audit fixes/features (SC1/SC2, SS1/SS3, SN1/SN8/SN9)."""
from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.models import Node, Subscription, TuningProfile, RoutingRule
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.backup import export_state, import_state


def _store(tmp_path):
    conn = connect(str(tmp_path / "t.sqlite"))
    init_schema(conn)
    return NodeStore(conn)


# --- SC1: backup round-trip preserves the NEW fields (the regression this audit found) ---
def test_backup_roundtrip_preserves_new_fields(tmp_path):
    src = _store(tmp_path)
    pid = src.add_profile(TuningProfile(id=None, name="hard", noise_enabled=True,
                          noises=[{"type": "rand", "packet": "50-150", "delay": "10-16"}],
                          xhttp_padding="100-1000", alpn="h2", tls_min="1.3"))
    sid = src.add_subscription(Subscription(id=None, name="s", url="u", enabled=False,
                                            default_profile_id=pid))
    nid = src.add_node(Node(id=None, name="x", address="1.2.3.4", port=443, uuid="u",
                            transport="xhttp", network="xhttp", security="tls", path="/dl",
                            subscription_id=sid, tuning_profile_id=pid))
    src.replace_routing([RoutingRule(id=None, position=0, type="domain", value="x.com",
                                     action="block", enabled=False, label="ads")])
    doc = export_state(src)
    assert doc["schema_version"] == 2

    dst = _store(tmp_path / "dst")
    import_state(dst, doc)
    n = dst.get_node(nid)
    assert n.network == "xhttp" and n.security == "tls" and n.path == "/dl"   # node stream fields kept
    p = dst.get_profile(pid)
    assert p.noise_enabled is True and p.noises[0]["type"] == "rand"          # tuning anti-DPI kept
    assert p.xhttp_padding == "100-1000" and p.alpn == "h2" and p.tls_min == "1.3"
    s = dst.get_subscription(sid)
    assert s.enabled is False and s.default_profile_id == pid                 # sub lifecycle kept
    r = dst.get_routing()[0]
    assert r.enabled is False and r.label == "ads"                           # routing flags kept


def test_backup_v1_back_compat_rederives_node(tmp_path):
    # a schema-1 backup of an xhttp node (no network/security stored) restores coherently
    dst = _store(tmp_path)
    v1 = {"schema_version": 1, "profiles": [], "subscriptions": [],
          "nodes": [{"id": 1, "name": "x", "address": "a", "port": 443, "uuid": "u",
                     "transport": "xhttp", "public_key": "", "flow": "xtls-rprx-vision",
                     "subscription_id": None, "stale": False, "tuning_profile_id": None}],
          "routing": {"rules": [], "default_action": "proxy"}, "settings": {}}
    import_state(dst, v1)
    n = dst.get_node(1)
    assert n.network == "xhttp" and n.flow == "" and n.security == "tls"      # normalize() recovered it


# --- API ---
def _client(settings, stub_xray):
    settings.xray_bin = stub_xray
    return TestClient(create_app(settings, state=build_state(settings, net=DryRunBackend())))


def _login(c):
    c.post("/api/setup", json={"username": "admin", "password": "changeme"})
    return c.get("/api/csrf").json()["csrf"]


def test_settings_validation_rejects_busyloop_values(settings, stub_xray):
    c = _client(settings, stub_xray); h = {"X-CSRF-Token": _login(c)}
    assert c.put("/api/settings", json={"health_interval": 0}, headers=h).status_code == 422
    assert c.put("/api/settings", json={"traffic_sample_ms": 10}, headers=h).status_code == 422
    assert c.put("/api/settings", json={"stats_api_port": 70000}, headers=h).status_code == 422


def test_settings_new_fields_and_reset(settings, stub_xray):
    c = _client(settings, stub_xray); h = {"X-CSRF-Token": _login(c)}
    s = c.get("/api/settings").json()
    assert s["session_timeout_min"] == 0 and s["auto_backup_enabled"] is False
    c.put("/api/settings", json={"auto_backup_enabled": True, "session_timeout_min": 30}, headers=h)
    assert c.get("/api/settings").json()["auto_backup_enabled"] is True
    r = c.post("/api/settings/reset", headers=h)
    assert r.json()["auto_backup_enabled"] is False and r.json()["session_timeout_min"] == 0


def test_diagnostics(settings, stub_xray):
    c = _client(settings, stub_xray); _login(c)
    d = c.get("/api/diagnostics").json()
    assert "app_version" in d and d["uptime_sec"] >= 0 and d["disk_total_bytes"] > 0


def test_password_change_invalidates_other_sessions(settings, stub_xray):
    # session A changes password → session B (separate client) is signed out
    a = _client(settings, stub_xray)
    a.post("/api/setup", json={"username": "admin", "password": "firstpass"})
    # second client logs in independently
    b = TestClient(a.app)
    assert b.post("/api/login", json={"username": "admin", "password": "firstpass"}).status_code == 200
    assert b.get("/api/csrf").status_code == 200
    ha = {"X-CSRF-Token": a.get("/api/csrf").json()["csrf"]}
    assert a.post("/api/password", json={"current_password": "firstpass",
                  "new_password": "secondpass"}, headers=ha).status_code == 200
    assert b.get("/api/csrf").status_code == 401          # B's session is now invalid
    assert a.get("/api/csrf").status_code == 200          # A stays valid


def test_auto_backup_writes_when_enabled(settings, stub_xray):
    import os
    from pi_gw_panel.backup.scheduler import BackupScheduler
    state = build_state(settings, net=DryRunBackend())
    sched = BackupScheduler(state)
    assert sched.run_once(now=1) is None                  # disabled → no-op
    state.store.set_setting("auto_backup_enabled", "1")
    path = sched.run_once(now=2)
    assert path is not None and os.path.exists(path)
