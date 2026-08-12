import json
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
from pi_gw_panel.xray_config.validate import config_digest


def _store():
    conn = connect(":memory:")
    init_schema(conn)
    return NodeStore(conn)


class _Running:
    def status(self):
        return {"running": True, "pid": 42}


_CONFIG = {"inbounds": [{"tag": "live"}]}


class _Xray:
    """The three things readiness reads off the supervisor: whether a process is up, the digest
    of the config that process loaded, and where that config lives.

    Readiness asks two different questions here — does a process exist (`xray`), and is that
    process serving the config on disk (`xray_config`) — so a stub that answers only the first
    leaves the second unestablished, which is a state this test module has to be able to build.
    """

    def __init__(self, config_path: str, loaded: str | None, running: bool = True):
        self.config_path, self._loaded, self._running = config_path, loaded, running

    def status(self):
        return {"running": self._running, "pid": 42 if self._running else None,
                "loaded_config_digest": self._loaded}


def _ready_state(tmp_path, *, loaded="match", running=True):
    """A gateway with every layer ready.

    `loaded` is the digest the supervisor reports for the config the live process loaded:
    "match" is the digest of the file written here — a process whose configuration has been
    verified — and None is one whose configuration cannot be established at all.
    """
    config_path = tmp_path / "xray.json"
    config_path.write_text(json.dumps(_CONFIG))
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
        supervisor=_Xray(
            str(config_path),
            config_digest(_CONFIG) if loaded == "match" else loaded,
            running=running),
        dnsmasq=_Running(),
        provision_result=NetResult(ok=True),
    )


def test_readiness_checks_every_required_gateway_layer(tmp_path):
    state = _ready_state(tmp_path)
    checks = netcheck.readiness_checks(
        state, address_reader=lambda _iface: {"192.168.10.2/24"})
    assert checks == {
        "provisioning": True,
        "segment_addresses": True,
        "dnsmasq": True,
        "enforcement": True,
        "active_node": True,
        "xray": True,
        # A running process is not the same as a process serving the config on disk: this one
        # loaded the digest the file hashes to, so the comparison ran and matched. The other
        # answers ("drift", and "unknown" on either side of the running flag) are the matrix
        # below — see test_config_drift_audit.py for the whole vocabulary.
        "xray_config": True,
        "tunnel": True,
    }


def test_readiness_fails_for_stale_tunnel_and_missing_managed_v6(tmp_path):
    state = _ready_state(tmp_path)
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


def test_readiness_reports_an_unexpected_extra_address_as_drift(caplog, tmp_path):
    # The orphan a partially-applied reconcile can strand: a subset test never sees it, so it
    # would stay invisible to the panel forever.
    state = _ready_state(tmp_path)
    checks = netcheck.readiness_checks(
        state, address_reader=lambda _iface: {"192.168.10.2/24", "192.168.44.2/24"})
    assert checks["segment_addresses"] is False
    assert "192.168.44.2/24" in caplog.text


def test_readiness_reports_which_address_drifted_and_not_only_that_it_did(tmp_path):
    # The boolean alone sent the operator to the server log for the one fact that decides the
    # repair: whether an address is stranded or a recorded one is gone.
    state = _ready_state(tmp_path)
    details: dict[str, str] = {}
    checks = netcheck.readiness_checks(
        state, address_reader=lambda _iface: {"192.168.10.2/24", "192.168.44.2/24"},
        details=details)
    assert checks["segment_addresses"] is False
    assert "192.168.44.2/24" in details["segment_addresses"]      # the drifted address itself
    assert "eth0.2" in details["segment_addresses"]               # and where it is
    assert set(checks) == {"provisioning", "segment_addresses", "dnsmasq", "enforcement",
                           "active_node", "xray", "xray_config", "tunnel"}   # names unchanged


def test_readiness_details_name_a_missing_managed_address(tmp_path):
    state = _ready_state(tmp_path)
    details: dict[str, str] = {}
    checks = netcheck.readiness_checks(state, address_reader=lambda _iface: set(), details=details)
    assert checks["segment_addresses"] is False
    assert "missing 192.168.10.2/24" in details["segment_addresses"]


def test_readiness_details_stay_empty_when_every_layer_is_ready(tmp_path):
    state = _ready_state(tmp_path)
    details: dict[str, str] = {}
    checks = netcheck.readiness_checks(
        state, address_reader=lambda _iface: {"192.168.10.2/24"}, details=details)
    assert all(checks.values())
    assert details == {}          # a detail is a reason for a failure, never noise on success


def test_readiness_ignores_kernel_owned_link_local_addresses(tmp_path):
    state = _ready_state(tmp_path)
    checks = netcheck.readiness_checks(
        state, address_reader=lambda _iface: {"192.168.10.2/24", "fe80::1/64"})
    assert checks["segment_addresses"] is True


def test_readiness_treats_unmanaged_segment_as_not_applicable(tmp_path):
    # `manage_segment=0` (the host provisions the segment) is a supported mode. Reporting it as
    # a failed check pins /api/ready at 503 and makes the migration script roll back a healthy
    # cutover.
    state = _ready_state(tmp_path)
    state.store.set_setting("manage_segment", "0")
    checks = netcheck.readiness_checks(state, address_reader=lambda _iface: set())
    assert checks["segment_addresses"] is True


def test_readiness_host_probe_errors_fail_closed(tmp_path):
    state = _ready_state(tmp_path)

    def unavailable(_iface):
        raise OSError("ip command unavailable")

    checks = netcheck.readiness_checks(state, address_reader=unavailable)
    assert checks["segment_addresses"] is False


# --- `xray_config`: a LIVE process has to be verified, not merely unproven ------------------
#
# The check answers "is this gateway serving the configuration that is on disk". Before anything
# has been started there is nothing to compare, and failing there would pin /api/ready at 503 on
# a healthy boot; once a process IS up, "cannot tell" is not the same answer as "matches", and a
# migration that commits on it commits on a configuration nobody verified. The four cells of that
# matrix are pinned below, `running` × the drift verdict.


def _xray_checks(state, details):
    return netcheck.readiness_checks(
        state, address_reader=lambda _iface: {"192.168.10.2/24"}, details=details)


def test_readiness_fails_a_running_xray_whose_config_could_not_be_verified(tmp_path):
    # Up, and nothing recorded for what it loaded (an unparseable config at load, a file that
    # cannot be read now): the comparison never happened, so "ready" would be a claim about a
    # live configuration nobody checked.
    state = _ready_state(tmp_path, loaded=None)
    details: dict[str, str] = {}
    checks = _xray_checks(state, details)
    assert checks["xray"] is True, "precondition: the process really is up"
    assert checks["xray_config"] is False, \
        "readiness passed a running gateway whose configuration was never established"
    assert "could not be verified" in details["xray_config"]      # what is unproven
    assert "restart xray" in details["xray_config"]                # and the way out of it


def test_readiness_passes_a_running_xray_serving_the_config_on_disk(tmp_path):
    state = _ready_state(tmp_path)                     # loaded digest == the file's digest
    details: dict[str, str] = {}
    checks = _xray_checks(state, details)
    assert checks["xray_config"] is True
    assert "xray_config" not in details, \
        "a detail is a reason for a failure, never noise on a verified gateway"


def test_readiness_fails_a_running_xray_serving_a_different_config(tmp_path):
    # The proven divergence the check was added for — unchanged by the stricter unknown case.
    state = _ready_state(tmp_path, loaded=config_digest({"inbounds": [{"tag": "superseded"}]}))
    details: dict[str, str] = {}
    checks = _xray_checks(state, details)
    assert checks["xray"] is True, "the process exists — that check keeps its own meaning"
    assert checks["xray_config"] is False
    assert "different from the configuration on disk" in details["xray_config"]


def test_readiness_keeps_xray_config_green_when_nothing_has_been_started(tmp_path):
    """The healthy boot: a good config on disk and no process that has loaded it.

    Nothing is serving anything, so there is no unverified live configuration to fail on — and
    failing here would pin /api/ready at 503 on every boot and make the host migration script
    roll a good cutover back. The missing process is `xray`'s failure, reported once.
    """
    state = _ready_state(tmp_path, loaded=None, running=False)
    details: dict[str, str] = {}
    checks = _xray_checks(state, details)
    assert checks["xray"] is False, "the check that IS about the process carries that failure"
    assert checks["xray_config"] is True, \
        "a boot with nothing started was failed for a comparison that cannot have happened yet"
    assert "xray_config" not in details


def test_readiness_keeps_xray_config_green_for_an_unparseable_config_with_nothing_running(
        tmp_path):
    # A config that cannot even be hashed, and nothing running: still not a live process serving
    # something unverified, so the boot stays green on this check.
    state = _ready_state(tmp_path, loaded=None, running=False)
    (tmp_path / "xray.json").write_text('{"inbounds": [')      # truncated by a crash mid-write
    details: dict[str, str] = {}
    checks = _xray_checks(state, details)
    assert checks["xray_config"] is True
    assert "xray_config" not in details


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
