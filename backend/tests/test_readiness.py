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
        # A running process is not the same as a process serving the config on disk. This
        # supervisor records no loaded digest, i.e. "unknown", which is not a divergence — see
        # test_config_drift_audit.py for the whole vocabulary.
        "xray_config": True,
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


def test_readiness_reports_an_unexpected_extra_address_as_drift(caplog):
    # The orphan a partially-applied reconcile can strand: a subset test never sees it, so it
    # would stay invisible to the panel forever.
    state = _ready_state()
    checks = netcheck.readiness_checks(
        state, address_reader=lambda _iface: {"192.168.10.2/24", "192.168.44.2/24"})
    assert checks["segment_addresses"] is False
    assert "192.168.44.2/24" in caplog.text


def test_readiness_reports_which_address_drifted_and_not_only_that_it_did():
    # The boolean alone sent the operator to the server log for the one fact that decides the
    # repair: whether an address is stranded or a recorded one is gone.
    state = _ready_state()
    details: dict[str, str] = {}
    checks = netcheck.readiness_checks(
        state, address_reader=lambda _iface: {"192.168.10.2/24", "192.168.44.2/24"},
        details=details)
    assert checks["segment_addresses"] is False
    assert "192.168.44.2/24" in details["segment_addresses"]      # the drifted address itself
    assert "eth0.2" in details["segment_addresses"]               # and where it is
    assert set(checks) == {"provisioning", "segment_addresses", "dnsmasq", "enforcement",
                           "active_node", "xray", "xray_config", "tunnel"}   # names unchanged


def test_readiness_details_name_a_missing_managed_address():
    state = _ready_state()
    details: dict[str, str] = {}
    checks = netcheck.readiness_checks(state, address_reader=lambda _iface: set(), details=details)
    assert checks["segment_addresses"] is False
    assert "missing 192.168.10.2/24" in details["segment_addresses"]


def test_readiness_details_stay_empty_when_every_layer_is_ready():
    state = _ready_state()
    details: dict[str, str] = {}
    checks = netcheck.readiness_checks(
        state, address_reader=lambda _iface: {"192.168.10.2/24"}, details=details)
    assert all(checks.values())
    assert details == {}          # a detail is a reason for a failure, never noise on success


def test_readiness_ignores_kernel_owned_link_local_addresses():
    state = _ready_state()
    checks = netcheck.readiness_checks(
        state, address_reader=lambda _iface: {"192.168.10.2/24", "fe80::1/64"})
    assert checks["segment_addresses"] is True


def test_readiness_treats_unmanaged_segment_as_not_applicable():
    # `manage_segment=0` (the host provisions the segment) is a supported mode. Reporting it as
    # a failed check pins /api/ready at 503 and makes the migration script roll back a healthy
    # cutover.
    state = _ready_state()
    state.store.set_setting("manage_segment", "0")
    checks = netcheck.readiness_checks(state, address_reader=lambda _iface: set())
    assert checks["segment_addresses"] is True


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

    monkeypatch.setattr(netcheck, "readiness_checks",
                        lambda _state, details=None: {k: False for k in names})
    response = client.get("/api/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"]["provisioning"] is False

    monkeypatch.setattr(netcheck, "readiness_checks",
                        lambda _state, details=None: {k: True for k in names})
    response = client.get("/api/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {k: True for k in names},
                               "details": {}}


def test_ready_route_returns_the_drift_detail_beside_the_boolean(
        settings, stub_xray, monkeypatch):
    settings.xray_bin = stub_xray
    state = build_state(settings, net=DryRunBackend())
    client = TestClient(create_app(settings, state=state))
    names = ("provisioning", "segment_addresses", "dnsmasq", "enforcement",
             "active_node", "xray", "tunnel")

    def drifted(_state, details=None):
        if details is not None:
            details["segment_addresses"] = "eth0.2: unexpected 192.168.44.2/24"
        return {k: k != "segment_addresses" for k in names}

    monkeypatch.setattr(netcheck, "readiness_checks", drifted)
    response = client.get("/api/ready")
    assert response.status_code == 503                          # still fail-closed
    body = response.json()
    assert body["checks"] == {k: k != "segment_addresses" for k in names}   # names + meaning kept
    assert body["details"] == {"segment_addresses": "eth0.2: unexpected 192.168.44.2/24"}
