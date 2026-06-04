from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.health import probe


# --- probe.http_ping (TLS-handshake reachability) ---
def test_http_ping_ok_measures_latency():
    class _Conn:
        def close(self): pass
    seen = []
    def fake_connect(addr, timeout):
        seen.append((addr, timeout)); return _Conn()
    clock = iter([1.0, 1.25])
    ok, ms = probe.http_ping("1.2.3.4", 443, "sni.example",
                             connect=fake_connect, clock=lambda: next(clock))
    assert ok is True and ms == 250
    assert seen == [(("1.2.3.4", 443), 5.0)]


def test_http_ping_fail_returns_none():
    def boom(addr, timeout): raise OSError("handshake failed")
    ok, ms = probe.http_ping("1.2.3.4", 443, "sni", connect=boom)
    assert ok is False and ms is None


# --- /api/probe/{tcp,http} sweeps ---
def _client(settings, stub_xray):
    settings.xray_bin = stub_xray
    return TestClient(create_app(settings, state=build_state(settings, net=DryRunBackend())))


def _login(c):
    c.post("/api/setup", json={"username": "admin", "password": "changeme"})
    return c.get("/api/csrf").json()["csrf"]


def _add(c, tok, name):
    body = {"name": name, "address": "1.2.3.4", "port": 443, "uuid": f"u-{name}",
            "sni": "x", "public_key": "PK", "short_id": "ab"}
    return c.post("/api/nodes", json=body, headers={"X-CSRF-Token": tok}).json()["id"]


def test_probe_tcp_sweep_updates_all_nodes(settings, stub_xray, monkeypatch):
    monkeypatch.setattr(probe, "tcp_ping", lambda a, p, **k: (True, 12))
    c = _client(settings, stub_xray); tok = _login(c)
    n1, n2 = _add(c, tok, "a"), _add(c, tok, "b")
    r = c.post("/api/probe/tcp", headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    h = {x["node_id"]: x for x in r.json()}
    assert h[n1]["last_tcp_ok"] is True and h[n1]["last_tcp_ms"] == 12
    assert h[n2]["last_tcp_ok"] is True


def test_probe_http_sweep_updates_real_fields(settings, stub_xray, monkeypatch):
    monkeypatch.setattr(probe, "http_ping", lambda a, p, sni, **k: (True, 34))
    c = _client(settings, stub_xray); tok = _login(c)
    nid = _add(c, tok, "a")
    r = c.post("/api/probe/http", headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    h = {x["node_id"]: x for x in r.json()}
    assert h[nid]["last_real_ok"] is True and h[nid]["last_real_ms"] == 34


def test_probe_needs_csrf(settings, stub_xray):
    c = _client(settings, stub_xray); _login(c)
    assert c.post("/api/probe/tcp").status_code == 403
