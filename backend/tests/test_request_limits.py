from fastapi.testclient import TestClient

from pi_gw_panel.app import AUTH_BODY_LIMIT, create_app
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.state import build_state


def _client(settings, stub_xray):
    settings.xray_bin = stub_xray
    return TestClient(create_app(settings, state=build_state(settings, net=DryRunBackend())))


def test_oversized_content_length_is_rejected_before_login(settings, stub_xray, monkeypatch):
    client = _client(settings, stub_xray)
    calls = []
    monkeypatch.setattr("pi_gw_panel.auth.service.verify_login", lambda *_: calls.append(1) or False)
    response = client.post(
        "/api/login", content=b"x" * (AUTH_BODY_LIMIT + 1),
        headers={"content-type": "application/json"})
    assert response.status_code == 413
    assert calls == []


def test_oversized_chunked_body_is_rejected(settings, stub_xray):
    client = _client(settings, stub_xray)

    def chunks():
        yield b"{" + b" " * (AUTH_BODY_LIMIT // 2)
        yield b" " * (AUTH_BODY_LIMIT // 2 + 1) + b"}"

    response = client.post(
        "/api/login", content=chunks(), headers={"content-type": "application/json"})
    assert response.status_code == 413


def test_body_at_limit_reaches_normal_validation(settings, stub_xray):
    client = _client(settings, stub_xray)
    response = client.post(
        "/api/login", content=b"x" * AUTH_BODY_LIMIT,
        headers={"content-type": "application/json"})
    assert response.status_code == 422
