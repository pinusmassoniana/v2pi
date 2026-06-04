from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.net_control.dryrun import DryRunBackend


def _client(settings, stub_xray):
    settings.xray_bin = stub_xray
    return TestClient(create_app(settings, state=build_state(settings, net=DryRunBackend())))


def _auth(c):
    c.post("/api/setup", json={"username": "admin", "password": "s3cret12"})
    return {"X-CSRF-Token": c.get("/api/csrf").json()["csrf"]}


def test_get_network_shape(settings, stub_xray):
    c = _client(settings, stub_xray)
    _auth(c)
    body = c.get("/api/network").json()
    assert body["segment"] == {"iface": "eth0.2", "ip": "192.168.10.2",
                               "dhcp_start": "192.168.10.30", "dhcp_end": "192.168.10.200",
                               "dhcp_lease": "12h", "client_dns": "1.1.1.1"}
    assert body["kill_switch_enabled"] is False
    assert body["status"]["segment_up"] is None             # dev: no Linux sysfs
    assert body["status"]["dhcp_clients"] == 0
    assert set(body["status"]["tunnel"]) == {"real_ok", "latency_ms", "egress_ip", "checked_at"}
    assert body["status"]["clients"] == []
    assert len(body["recommendations"]) >= 1


def test_put_network_requires_csrf(settings, stub_xray):
    c = _client(settings, stub_xray)
    _auth(c)
    assert c.put("/api/network", json={"dhcp_end": "192.168.10.250"}).status_code == 403


def test_put_network_updates_field_and_flips_killswitch(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = _auth(c)
    body = c.put("/api/network",
                 json={"dhcp_end": "192.168.10.250", "kill_switch_enabled": True}, headers=h).json()
    assert body["segment"]["dhcp_end"] == "192.168.10.250"   # field persisted
    assert body["kill_switch_enabled"] is True
    # A1: kill-switch ON with no tunnel up installs the fail-closed leak-guard (forward drop,
    # NO tproxy pointed at a dead xray port), not the full tproxy render.
    applied = c.app.state.app_state.net.applied[-1]
    assert "chain forward" in applied and " drop" in applied
    assert "tproxy ip to" not in applied


def test_put_network_rejects_empty_and_ignores_unknown(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = _auth(c)
    assert c.put("/api/network", json={"segment_iface": ""}, headers=h).status_code == 422
    r = c.put("/api/network", json={"bogus": "x"}, headers=h)       # unknown keys ignored
    assert r.status_code == 200 and r.json()["segment"]["iface"] == "eth0.2"
