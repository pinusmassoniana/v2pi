"""Locks the new contracts introduced by the 2026-07-10 full GUI+backend audit fixes."""
import pytest
from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.models import RoutingRule
from pi_gw_panel.xray_config.routing import _rule_to_field
from pi_gw_panel.subs import fetcher
from pi_gw_panel.auth import service as auth_service
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore


def _client(settings, stub_xray):
    settings.xray_bin = stub_xray
    return TestClient(create_app(settings, state=build_state(settings, net=DryRunBackend())))


def _login(c):
    c.post("/api/setup", json={"username": "admin", "password": "changeme123"})
    return c.get("/api/csrf").json()["csrf"]


def _store(settings) -> NodeStore:
    conn = connect(settings.db_path, check_same_thread=False)
    init_schema(conn)
    return NodeStore(conn)


# --- SSRF host guard (BF/BA): private/link-local/loopback rejected, public allowed ---

def test_host_blocked_rejects_internal_and_allows_public():
    for bad in ("127.0.0.1", "localhost", "10.1.2.3", "192.168.1.1", "169.254.169.254", "::1", ""):
        assert fetcher.host_blocked(bad) is True
    assert fetcher.host_blocked("8.8.8.8") is False        # public literal always allowed


def test_assert_public_url_scheme_and_host():
    with pytest.raises(ValueError):
        fetcher.assert_public_url("file:///etc/passwd")     # scheme
    with pytest.raises(ValueError):
        fetcher.assert_public_url("http://169.254.169.254/latest/meta-data")  # metadata host


# --- auth (BB): non-ASCII username can't 500; wrong user != crash ---

def test_verify_login_non_ascii_username_is_safe(settings):
    store = _store(settings)
    auth_service.create_credential(store, "админ", "s3cret12")
    assert auth_service.verify_login(store, "админ", "s3cret12") is True    # would raise pre-fix
    assert auth_service.verify_login(store, "wrong", "s3cret12") is False
    assert auth_service.verify_login(store, "админ", "bad") is False


def test_set_password_bumps_session_epoch(settings):
    store = _store(settings)
    auth_service.create_credential(store, "admin", "s3cret12")
    e0 = auth_service.session_epoch(store)
    auth_service.set_password(store, "newpass12")
    assert auth_service.session_epoch(store) == e0 + 1      # other sessions invalidated


# --- routing (BE): a multi-line port value is normalized, not emitted with a newline ---

def test_port_rule_normalizes_newlines():
    field = _rule_to_field(RoutingRule(id=None, position=0, type="port", value="80\n443", action="direct"))
    assert field["port"] == "80,443"


# --- net field validation (BD/BA): injection/malformed values rejected at the API boundary ---

def test_put_network_rejects_injection_and_bad_values(settings, stub_xray):
    c = _client(settings, stub_xray)
    tok = _login(c)
    h = {"X-CSRF-Token": tok}
    assert c.put("/api/network", json={"segment_iface": "eth0\ninject"}, headers=h).status_code == 422
    assert c.put("/api/network", json={"client_dns": "not-an-ip"}, headers=h).status_code == 422
    assert c.put("/api/network", json={"dhcp_lease": "12h; rm -rf"}, headers=h).status_code == 422
    # a well-formed change is accepted
    assert c.put("/api/network", json={"client_dns": "1.1.1.1"}, headers=h).status_code == 200


# --- settings (BA): routing_default_action is validated ---

def test_put_settings_rejects_bad_routing_default(settings, stub_xray):
    c = _client(settings, stub_xray)
    tok = _login(c)
    r = c.put("/api/settings", json={"routing_default_action": "bogus"}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 422


# --- restore (BA): a non-backup document is refused before the destructive overwrite ---

def test_restore_rejects_non_backup(settings, stub_xray):
    c = _client(settings, stub_xray)
    tok = _login(c)
    r = c.post("/api/restore", json={"just": "settings-export"}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 400


# --- logout (BA): now CSRF-protected ---

def test_logout_requires_csrf(settings, stub_xray):
    c = _client(settings, stub_xray)
    _login(c)
    assert c.post("/api/logout").status_code == 403             # no token → refused
    tok = c.get("/api/csrf").json()["csrf"]
    assert c.post("/api/logout", headers={"X-CSRF-Token": tok}).status_code == 200


# --- supervisor (BE): reload reports whether xray came up ---

def test_supervisor_reload_returns_true_when_alive(stub_xray, tmp_path):
    from pi_gw_panel.xray_supervisor.supervisor import XraySupervisor
    cfg = tmp_path / "xray.json"
    cfg.write_text("{}")
    sup = XraySupervisor(stub_xray, str(cfg))
    try:
        assert sup.reload() is True        # stub sleeps → alive after start
    finally:
        sup.stop()
