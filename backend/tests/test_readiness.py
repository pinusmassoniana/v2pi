from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from pi_gw_panel.app import create_app
from pi_gw_panel.config import Settings
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.models import Node, NodeHealth
from pi_gw_panel.net_control import netcheck
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.net_control.plan import NetResult
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.state import build_state


def _store():
    conn = connect(":memory:")
    init_schema(conn)
    return NodeStore(conn)


class _Running:
    def status(self):
        return {"running": True, "pid": 42}


def _ready_state():
    store = _store()
    node_id = store.add_node(Node(id=None, name="ready", address="1.1.1.1", port=443, uuid="u"))
    store.set_setting("active_node_id", str(node_id))
    store.set_setting("managed_segment_iface", "eth0.2")
    store.set_setting("managed_segment_addr4", "192.168.10.2/24")
    store.upsert_health(NodeHealth(
        node_id=node_id,
        last_real_ok=True,
        checked_at=datetime.now(timezone.utc).isoformat(),
    ))
    net = SimpleNamespace(
        enforcement_status="ok", enforcement_error="", wan_blocked=False)
    return SimpleNamespace(
        store=store,
        settings=Settings(),
        net=net,
        supervisor=_Running(),
        dnsmasq=_Running(),
        provision_result=NetResult(ok=True),
    )


def test_readiness_checks_every_required_gateway_layer():
    state = _ready_state()
    checks = netcheck.readiness_checks(
        state, address_reader=lambda _iface: {"192.168.10.2/24"})
    assert checks == {
        "provisioning": True,
        "segment_addresses": True,
        "dnsmasq": True,
        "enforcement": True,
        "active_node": True,
        "xray": True,
        "tunnel": True,
    }


def test_readiness_fails_for_stale_tunnel_and_missing_managed_v6():
    state = _ready_state()
    state.store.set_setting("ipv6_enabled", "1")
    state.store.set_setting("managed_segment_addr6", "fd00:1:2:3::1/64")
    node_id = int(state.store.get_setting("active_node_id"))
    state.store.upsert_health(NodeHealth(
        node_id=node_id,
        last_real_ok=True,
        checked_at="2020-01-01T00:00:00+00:00",
    ))
    checks = netcheck.readiness_checks(
        state, address_reader=lambda _iface: {"192.168.10.2/24"})
    assert checks["segment_addresses"] is False
    assert checks["tunnel"] is False


def test_readiness_host_probe_errors_fail_closed():
    state = _ready_state()

    def unavailable(_iface):
        raise OSError("ip command unavailable")

    checks = netcheck.readiness_checks(state, address_reader=unavailable)
    assert checks["segment_addresses"] is False


def test_ready_route_is_open_and_uses_503_until_all_checks_pass(
        settings, stub_xray, monkeypatch):
    settings.xray_bin = stub_xray
    state = build_state(settings, net=DryRunBackend())
    client = TestClient(create_app(settings, state=state))
    names = ("provisioning", "segment_addresses", "dnsmasq", "enforcement",
             "active_node", "xray", "tunnel")

    monkeypatch.setattr(netcheck, "readiness_checks", lambda _state: {k: False for k in names})
    response = client.get("/api/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"]["provisioning"] is False

    monkeypatch.setattr(netcheck, "readiness_checks", lambda _state: {k: True for k in names})
    response = client.get("/api/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {k: True for k in names}}
