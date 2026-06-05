"""Backend coverage for the 2026-06-05 self-provisioning gateway work (v1.10).

Phase A — the panel owns DHCP/RA: build_state wires a DnsmasqSupervisor; client_dns6 is editable
and surfaced. Phase C — foreign_ra (rogue-RA) is part of the /network status contract."""
from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.net_control.dryrun import DryRunBackend


def _client(settings, stub_xray):
    settings.xray_bin = stub_xray
    state = build_state(settings, net=DryRunBackend())
    c = TestClient(create_app(settings, state=state))
    c.post("/api/setup", json={"username": "admin", "password": "s3cret12"})
    return c, {"X-CSRF-Token": c.get("/api/csrf").json()["csrf"]}, state


def test_build_state_wires_dnsmasq_and_pd(settings):
    state = build_state(settings, net=DryRunBackend())
    assert state.dnsmasq is not None and state.dnsmasq.conf_path.endswith("dnsmasq.conf")
    assert state.pd_client is not None


def test_network_segment_out_has_client_dns6(settings, stub_xray):
    c, h, _ = _client(settings, stub_xray)
    body = c.get("/api/network").json()
    assert body["segment"]["client_dns6"] == "2606:4700:4700::1111"


def test_put_network_accepts_client_dns6(settings, stub_xray):
    c, h, _ = _client(settings, stub_xray)
    body = c.put("/api/network", json={"client_dns6": "2001:4860:4860::8888"}, headers=h).json()
    assert body["segment"]["client_dns6"] == "2001:4860:4860::8888"


def test_network_status_has_foreign_ra_and_source_fields(settings, stub_xray):
    c, h, _ = _client(settings, stub_xray)
    body = c.get("/api/network").json()
    assert "foreign_ra" in body["status"]            # DryRun -> None, but present in the contract
    assert "ipv6_prefix_source" in body["status"]
