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


def test_probe_http_sweep_updates_http_fields(settings, stub_xray, monkeypatch):
    monkeypatch.setattr(probe, "http_ping", lambda a, p, sni, **k: (True, 34))
    c = _client(settings, stub_xray); tok = _login(c)
    nid = _add(c, tok, "a")
    r = c.post("/api/probe/http", headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    h = {x["node_id"]: x for x in r.json()}
    assert h[nid]["last_http_ok"] is True and h[nid]["last_http_ms"] == 34


# --- per-node real-through-node probe (the "T" button) ---
def test_probe_outbound_xhttp_and_reality():
    from pi_gw_panel.models import Node
    x = Node(id=1, name="x", address="a", port=443, uuid="u", transport="xhttp", network="xhttp",
             security="tls", sni="s", path="/p", host="h", mode="stream-up", alpn="h2,http/1.1")
    sx = probe._probe_outbound(x)["streamSettings"]
    assert sx["network"] == "xhttp" and sx["security"] == "tls"
    assert sx["xhttpSettings"]["path"] == "/p" and sx["tlsSettings"]["alpn"] == ["h2", "http/1.1"]
    r = Node(id=2, name="r", address="b", port=443, uuid="u2", transport="vision", network="tcp",
             security="reality", sni="t", public_key="PK", short_id="sid", flow="xtls-rprx-vision")
    o = probe._probe_outbound(r)
    assert o["streamSettings"]["realitySettings"]["publicKey"] == "PK"
    assert o["settings"]["vnext"][0]["users"][0]["flow"] == "xtls-rprx-vision"


def test_real_through_node_spawns_and_probes(monkeypatch):
    import json as _j
    from pi_gw_panel.models import Node
    n = Node(id=1, name="n", address="aa", port=443, uuid="u", transport="vision",
             network="tcp", security="reality", sni="s", public_key="PK", short_id="x")
    spawned = []
    class _Proc:
        def terminate(self): pass
        def wait(self, timeout=None): pass
    def fake_spawn(path):
        spawned.append(_j.load(open(path))); return _Proc()
    monkeypatch.setattr(probe, "real_request", lambda proxy, url, timeout=5.0: (True, 200, 77, "9.9.9.9"))
    ok, ms, egress, egress6 = probe.real_through_node(n, "xray", "https://probe",
                                                      spawn=fake_spawn, wait_ready=lambda p: None)
    assert ok is True and ms == 77 and egress == "9.9.9.9" and egress6 is None
    cfg = spawned[0]
    assert cfg["inbounds"][0]["protocol"] == "http"
    assert cfg["outbounds"][0]["settings"]["vnext"][0]["address"] == "aa"


def test_probe_node_endpoint_runs_all_three(settings, stub_xray, monkeypatch):
    monkeypatch.setattr(probe, "tcp_ping", lambda a, p, **k: (True, 5))
    monkeypatch.setattr(probe, "http_ping", lambda a, p, sni, **k: (True, 9))
    monkeypatch.setattr(probe, "real_through_node", lambda node, xb, url, **k: (True, 42, "9.9.9.9", None))
    c = _client(settings, stub_xray); tok = _login(c)
    nid = _add(c, tok, "a")
    r = c.post(f"/api/nodes/{nid}/probe", headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    h = r.json()
    assert h["last_tcp_ms"] == 5 and h["last_http_ms"] == 9 and h["last_real_ms"] == 42 and h["egress_ip"] == "9.9.9.9"


def test_probe_needs_csrf(settings, stub_xray):
    c = _client(settings, stub_xray); _login(c)
    assert c.post("/api/probe/tcp").status_code == 403
