"""Regression contracts for candidate -> validate/apply -> commit API mutations."""

from fastapi.testclient import TestClient

from pi_gw_panel.app import create_app
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.state import build_state


def _active_client(settings, stub_xray):
    settings.xray_bin = stub_xray
    state = build_state(settings, net=DryRunBackend())
    client = TestClient(create_app(settings, state=state))
    assert client.post(
        "/api/setup", json={"username": "admin", "password": "changeme123"}).status_code == 200
    headers = {"X-CSRF-Token": client.get("/api/csrf").json()["csrf"]}
    node_id = client.post(
        "/api/nodes",
        json={"name": "n1", "address": "1.2.3.4", "port": 443, "uuid": "u-1"},
        headers=headers,
    ).json()["id"]
    assert client.post(f"/api/nodes/{node_id}/apply", headers=headers).status_code == 200
    return client, state, headers


def test_settings_candidate_rolls_back_when_xray_validation_fails(
        settings, stub_xray, monkeypatch):
    client, state, headers = _active_client(settings, stub_xray)
    try:
        before = state.store.get_setting("dns_intercept")
        monkeypatch.setenv("STUB_XRAY_FAIL", "1")
        response = client.put("/api/settings", json={"dns_intercept": True}, headers=headers)
        assert response.status_code == 502
        assert state.store.get_setting("dns_intercept") == before
        assert state.store.get_setting("active_node_id") == "1"
    finally:
        state.supervisor.stop()


def test_routing_candidate_rolls_back_as_one_unit(settings, stub_xray, monkeypatch):
    client, state, headers = _active_client(settings, stub_xray)
    try:
        monkeypatch.setenv("STUB_XRAY_FAIL", "1")
        response = client.put(
            "/api/routing",
            json={
                "rules": [{"type": "domain", "value": "example.com", "action": "direct"}],
                "default_action": "block",
                "domain_strategy": "AsIs",
            },
            headers=headers,
        )
        assert response.status_code == 502
        assert state.store.get_routing() == []
        assert state.store.get_setting("routing_default_action") is None
        assert state.store.get_setting("routing_domain_strategy") is None
    finally:
        state.supervisor.stop()


def test_live_profile_edit_rolls_back_row_on_apply_failure(settings, stub_xray, monkeypatch):
    client, state, headers = _active_client(settings, stub_xray)
    try:
        profile_id = state.store.get_default_profile().id
        monkeypatch.setenv("STUB_XRAY_FAIL", "1")
        response = client.patch(
            f"/api/profiles/{profile_id}", json={"frag_enabled": True}, headers=headers)
        assert response.status_code == 502
        assert state.store.get_profile(profile_id).frag_enabled is False
    finally:
        state.supervisor.stop()


def test_stats_client_reconfigures_only_after_successful_apply(
        settings, stub_xray, monkeypatch):
    client, state, headers = _active_client(settings, stub_xray)
    try:
        assert state.stats_client.status()["address"].endswith(":10085")
        assert client.put(
            "/api/settings", json={"stats_api_port": 10086}, headers=headers).status_code == 200
        assert state.stats_client.status()["address"].endswith(":10086")
        monkeypatch.setenv("STUB_XRAY_FAIL", "1")
        assert client.put(
            "/api/settings", json={"stats_api_port": 10087}, headers=headers).status_code == 502
        assert state.stats_client.status()["address"].endswith(":10086")
        assert state.store.get_setting("stats_api_port") == "10086"
    finally:
        state.supervisor.stop()
