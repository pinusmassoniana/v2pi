import importlib.metadata
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pi_gw_panel import __version__
from pi_gw_panel.app import create_app
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.state import build_state
from pi_gw_panel.__main__ import ensure_bootstrap_token


def test_public_version_comes_from_package_metadata():
    installed = {
        distribution.metadata.get("Version")
        for distribution in importlib.metadata.distributions(name="pi-gw-panel")
        if distribution.metadata.get("Version")
    }
    assert __version__ in installed


def test_logout_audit_keeps_pre_endpoint_actor(settings, stub_xray):
    settings.xray_bin = stub_xray
    client = TestClient(create_app(settings, state=build_state(settings, net=DryRunBackend())))
    assert client.post(
        "/api/setup", json={"username": "admin", "password": "changeme123"}).status_code == 200
    csrf = client.get("/api/csrf").json()["csrf"]
    assert client.post("/api/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.post(
        "/api/login", json={"username": "admin", "password": "changeme123"}).status_code == 200
    entry = next(e for e in client.get("/api/audit").json() if e["path"] == "/api/logout")
    assert entry["actor"] == "user:admin"
    assert entry["status"] == 200


def test_remote_first_run_requires_bootstrap_token(settings, stub_xray):
    settings.xray_bin = stub_xray
    settings.bind_host = "0.0.0.0"
    settings.tls_cert = "/tmp/test-cert"
    settings.tls_key = "/tmp/test-key"
    token = ensure_bootstrap_token(settings.data_dir)
    app = create_app(settings, state=build_state(settings, net=DryRunBackend()))
    client = TestClient(app)
    body = {"username": "admin", "password": "changeme123"}
    assert client.get("/api/setup").json()["bootstrap_required"] is True
    assert client.post("/api/setup", json=body).status_code == 403
    assert client.post(
        "/api/setup", json=body, headers={"X-Bootstrap-Token": "wrong"}).status_code == 403
    assert client.post(
        "/api/setup", json=body, headers={"X-Bootstrap-Token": token}).status_code == 200
    assert not (Path(settings.data_dir) / "bootstrap_token").exists()


def test_tls_mode_marks_session_cookie_secure(settings, stub_xray):
    settings.xray_bin = stub_xray
    settings.tls_cert = "/tmp/test-cert"
    settings.tls_key = "/tmp/test-key"
    app = create_app(settings, state=build_state(settings, net=DryRunBackend()))
    session = next(m for m in app.user_middleware if "Session" in m.cls.__name__)
    assert session.kwargs["https_only"] is True


def _authenticated_client(settings, stub_xray):
    settings.xray_bin = stub_xray
    client = TestClient(create_app(settings, state=build_state(settings, net=DryRunBackend())))
    client.post("/api/setup", json={"username": "admin", "password": "changeme123"})
    return client, {"X-CSRF-Token": client.get("/api/csrf").json()["csrf"]}


def test_mutation_schemas_reject_unknown_null_and_oversized_inputs(settings, stub_xray):
    client, headers = _authenticated_client(settings, stub_xray)
    node = client.post(
        "/api/nodes",
        json={"name": "n", "address": "1.2.3.4", "port": 443, "uuid": "u"},
        headers=headers,
    ).json()
    assert client.patch(
        f"/api/nodes/{node['id']}", json={"name": None}, headers=headers).status_code == 422
    assert client.patch(
        f"/api/nodes/{node['id']}", json={"tuning_profile_id": None}, headers=headers
    ).status_code == 200
    assert client.put(
        "/api/settings", json={"traffic_sample_mz": 1000}, headers=headers).status_code == 422
    assert client.post(
        "/api/nodes/reorder", json={"ids": list(range(501))}, headers=headers).status_code == 422


def test_routing_network_probe_and_cadence_validate_at_boundary(settings, stub_xray):
    client, headers = _authenticated_client(settings, stub_xray)
    assert client.put(
        "/api/routing", json={"rules": [], "domain_strategy": "Typo"},
        headers=headers).status_code == 422
    for bad in ("192.0.2.0/24", "2001:db8::/48"):
        assert client.put(
            "/api/network", json={"segment_ip6": bad}, headers=headers).status_code == 422
    assert client.post(
        "/api/probe/tcp?scope=does-not-exist", headers=headers).status_code == 422
    assert client.put(
        "/api/settings", json={"traffic_sample_ms": 499}, headers=headers).status_code == 422
    assert client.put(
        "/api/settings", json={"traffic_sample_ms": 500}, headers=headers).status_code == 200
    assert client.get("/api/traffic/history?window_sec=60").json()["interval_ms"] == 500


def test_node_validation_honors_selected_profile(settings, stub_xray, monkeypatch):
    client, headers = _authenticated_client(settings, stub_xray)
    profile_id = client.post(
        "/api/profiles", json={"name": "selected"}, headers=headers).json()["id"]
    seen = []

    def capture(node, _settings, _store):
        seen.append(node.tuning_profile_id)
        return {}

    monkeypatch.setattr("pi_gw_panel.api.routes.build_node_config", capture)
    monkeypatch.setattr("pi_gw_panel.api.routes.validate_config", lambda *_: (True, ""))
    body = {"name": "n", "address": "a", "port": 443, "uuid": "u",
            "tuning_profile_id": profile_id}
    assert client.post("/api/nodes/validate", json=body, headers=headers).status_code == 200
    assert seen == [profile_id]
    body["tuning_profile_id"] = 999999
    assert client.post("/api/nodes/validate", json=body, headers=headers).status_code == 422


def test_partial_startup_still_cleans_every_owned_resource(
        settings, stub_xray, monkeypatch):
    settings.xray_bin = stub_xray
    state = build_state(settings, net=DryRunBackend())
    events: list[str] = []

    class Component:
        def __init__(self, name: str, fail: bool = False):
            self.name = name
            self.fail = fail

        def start(self):
            events.append(f"start:{self.name}")
            if self.fail:
                raise RuntimeError(f"boom:{self.name}")

        async def stop(self):
            events.append(f"stop:{self.name}")

    scheduler = Component("scheduler")
    monitor = Component("monitor")
    liveness = Component("liveness", fail=True)
    backup = Component("backup")
    state.recorder = Component("recorder")
    state.dnsmasq = None
    state.pd_client = None

    monkeypatch.setattr("pi_gw_panel.app.SubScheduler", lambda _: scheduler)
    monkeypatch.setattr("pi_gw_panel.app.HealthMonitor", lambda _: monitor)
    monkeypatch.setattr("pi_gw_panel.app.LivenessLoop", lambda _: liveness)
    monkeypatch.setattr("pi_gw_panel.backup.scheduler.BackupScheduler", lambda _: backup)
    monkeypatch.setattr("pi_gw_panel.net_control.provision.host_provision", lambda _: None)
    monkeypatch.setattr("pi_gw_panel.controller.boot_guard", lambda _: None)
    monkeypatch.setattr("pi_gw_panel.controller.reapply_active_node", lambda _: None)

    app = create_app(settings, state=state)
    with pytest.raises(RuntimeError, match="boom:liveness"):
        with TestClient(app):
            pass

    assert events == [
        "start:scheduler", "start:monitor", "start:liveness",
        "stop:liveness", "stop:monitor", "stop:scheduler",
    ]
    with pytest.raises(Exception, match="closed"):
        state.store.get_setting("anything")
