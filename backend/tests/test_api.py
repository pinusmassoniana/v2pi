import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.net_control import events as conn_events


def _state(settings, stub_xray):
    # exercise the real build_state wiring, injecting the dry-run net + stub xray
    settings.xray_bin = stub_xray
    return build_state(settings, net=DryRunBackend())


def _client(settings, stub_xray):
    app = create_app(settings, state=_state(settings, stub_xray))
    return TestClient(app)


def test_health_is_open(settings, stub_xray):
    c = _client(settings, stub_xray)
    r = c.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_lifespan_starts_and_stops_health_monitor(settings, stub_xray):
    app = create_app(settings, state=_state(settings, stub_xray))
    # entering the context fires startup (scheduler + health monitor start); the
    # 30s first-tick sleep means no real probing happens during the test.
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200
        assert app.state.monitor is not None
    assert app.state.monitor._task is None        # stop() ran on shutdown


def _login(c):
    # first-run setup creates the credential AND opens a session; the full auth flow
    # (login, logout, re-setup-409, password change) is covered in test_api_wave3a.py
    c.post("/api/setup", json={"username": "admin", "password": "changeme"})
    return c.get("/api/csrf").json()["csrf"]


def test_status_and_nodes(settings, stub_xray):
    c = _client(settings, stub_xray)
    tok = _login(c)
    # status before any node
    st = c.get("/api/status").json()
    assert st["server_now"] > 0   # D4: Pi wall-clock for client clock-skew correction (varies)
    assert {k: v for k, v in st.items() if k != "server_now"} == {
        "running": False, "pid": None, "active_node_id": None,
        "xray_state": "stopped", "active_since": None, "last_failover_at": None,
        "prev_active_node_id": None,
        "tunnel_online": False, "active_health_fresh": False,
        "failover_ready": False, "eligible_standby_count": 0,
        "health_enabled": True, "failover_enabled": True, "failovers_24h": 0}
    # add a node (mutation -> needs csrf)
    body = {"name": "n1", "address": "1.2.3.4", "port": 47000, "uuid": "u-1",
            "sni": "www.microsoft.com", "public_key": "PK", "short_id": "ab12"}
    assert c.post("/api/nodes", json=body).status_code == 403            # no csrf
    r = c.post("/api/nodes", json=body, headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    nid = r.json()["id"]
    # list shows it
    nodes = c.get("/api/nodes").json()
    assert [n["id"] for n in nodes] == [nid]
    assert nodes[0]["name"] == "n1"


def test_status_reports_tunnel_failover_readiness_and_recent_count(settings, stub_xray):
    c = _client(settings, stub_xray)
    tok = _login(c)
    headers = {"X-CSRF-Token": tok}
    active = c.post(
        "/api/nodes",
        json={"name": "active", "address": "1.2.3.4", "port": 443, "uuid": "u-1"},
        headers=headers,
    ).json()["id"]
    standby = c.post(
        "/api/nodes",
        json={"name": "standby", "address": "2.3.4.5", "port": 443, "uuid": "u-2"},
        headers=headers,
    ).json()["id"]
    try:
        assert c.post(f"/api/nodes/{active}/apply", headers=headers).status_code == 200
        store = c.app.state.app_state.store
        now = time.time()
        checked_at = datetime.fromtimestamp(now, timezone.utc).isoformat()
        store.update_health_real(
            active, real_ok=True, real_ms=17, egress_ip="203.0.113.8",
            egress_ip6=None, checked_at=checked_at,
        )
        store.update_health_direct(
            standby, tcp_ok=True, tcp_ms=11, http_ok=False, http_ms=None,
            checked_at=checked_at,
        )
        conn_events.record(store, "failover", "recent", now=now - 60)
        conn_events.record(store, "failover", "old", now=now - 90000)

        status = c.get("/api/status").json()
        assert {key: status[key] for key in (
            "tunnel_online", "active_health_fresh", "failover_ready",
            "eligible_standby_count", "health_enabled", "failover_enabled",
            "failovers_24h",
        )} == {
            "tunnel_online": True,
            "active_health_fresh": True,
            "failover_ready": True,
            "eligible_standby_count": 1,
            "health_enabled": True,
            "failover_enabled": True,
            "failovers_24h": 1,
        }

        store.set_setting("failover_enabled", "0")
        disabled = c.get("/api/status").json()
        assert disabled["failover_enabled"] is False
        assert disabled["failover_ready"] is False
        assert disabled["eligible_standby_count"] == 1
    finally:
        c.app.state.app_state.supervisor.stop()


def test_apply_and_rollback(settings, stub_xray):
    c = _client(settings, stub_xray)
    tok = _login(c)
    body = {"name": "n1", "address": "1.2.3.4", "port": 47000, "uuid": "u-1",
            "sni": "www.microsoft.com", "public_key": "PK", "short_id": "ab12"}
    nid = c.post("/api/nodes", json=body, headers={"X-CSRF-Token": tok}).json()["id"]
    try:
        # apply (mutation -> csrf); stub xray validates ok, dryrun net records
        r = c.post(f"/api/nodes/{nid}/apply", headers={"X-CSRF-Token": tok})
        assert r.status_code == 200 and r.json()["ok"] is True
        # status now reflects running + active node
        st = c.get("/api/status").json()
        assert st["running"] is True
        assert st["active_node_id"] == nid
        # apply without csrf is refused
        assert c.post(f"/api/nodes/{nid}/apply").status_code == 403
        # rollback works
        assert c.post("/api/rollback", headers={"X-CSRF-Token": tok}).status_code == 200
    finally:
        c.app.state.app_state.supervisor.stop()


def test_apply_unknown_node_404(settings, stub_xray):
    c = _client(settings, stub_xray)
    tok = _login(c)
    assert c.post("/api/nodes/999/apply", headers={"X-CSRF-Token": tok}).status_code == 404


def test_static_mount_serves_index_when_present(settings, stub_xray, tmp_path):
    static = tmp_path / "spa"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>pi-gw</title>")
    settings.static_dir = str(static)
    app = create_app(settings, state=_state(settings, stub_xray))
    c = TestClient(app)
    # API still works
    assert c.get("/api/health").status_code == 200
    # root serves the SPA index
    r = c.get("/")
    assert r.status_code == 200
    assert "pi-gw" in r.text


def test_no_static_mount_when_unset(settings, stub_xray):
    c = _client(settings, stub_xray)  # static_dir == ""
    assert c.get("/api/health").status_code == 200
    assert c.get("/").status_code == 404  # nothing mounted


def test_rollback_reverts_active_node(settings, stub_xray):
    c = _client(settings, stub_xray)
    tok = _login(c)
    h = {"X-CSRF-Token": tok}
    a = c.post("/api/nodes", json={"name": "a", "address": "1.1.1.1", "port": 1, "uuid": "ua"},
               headers=h).json()["id"]
    b = c.post("/api/nodes", json={"name": "b", "address": "2.2.2.2", "port": 2, "uuid": "ub"},
               headers=h).json()["id"]
    try:
        c.post(f"/api/nodes/{a}/apply", headers=h)
        c.post(f"/api/nodes/{b}/apply", headers=h)
        assert c.get("/api/status").json()["active_node_id"] == b
        assert c.post("/api/rollback", headers=h).json()["ok"] is True
        assert c.get("/api/status").json()["active_node_id"] == a   # reverted to the prior apply
    finally:
        c.app.state.app_state.supervisor.stop()


def test_rollback_reload_failure_installs_disconnected_guard(
        settings, stub_xray, monkeypatch):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    state = c.app.state.app_state
    state.store.set_setting("prev_active_node_id", "1")
    monkeypatch.setattr(
        "pi_gw_panel.api.routes.ConfigManager.rollback", lambda _self: True)
    monkeypatch.setattr(state.supervisor, "reload", lambda: False)

    response = c.post("/api/rollback", headers=h)

    assert response.status_code == 502
    assert state.net.applied
    assert "tproxy ip to" not in state.net.applied[-1]
    assert " drop" in state.net.applied[-1]
