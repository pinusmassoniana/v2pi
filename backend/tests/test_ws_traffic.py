import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.models import Node, NodeHealth


def _client_state(settings, stub_xray):
    settings.xray_bin = stub_xray
    state = build_state(settings, net=DryRunBackend())
    return TestClient(create_app(settings, state=state)), state


def _auth(c):
    c.post("/api/setup", json={"username": "admin", "password": "changeme"})


class _FakeSampler:
    totals = {"proxy": {"up": 100, "down": 200}}

    def sample(self):
        return {"proxy": {"up_bps": 800.0, "down_bps": 1600.0}}


def test_ws_requires_auth(settings, stub_xray):
    c, _ = _client_state(settings, stub_xray)
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/api/ws/traffic"):
            pass


def test_ws_streams_frames(settings, stub_xray):
    c, state = _client_state(settings, stub_xray)
    _auth(c)
    state.sampler = _FakeSampler()
    state.recorder._sampler = state.sampler
    state.recorder.record_sample(state.sampler.sample())
    nid = state.store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u"))
    state.store.set_setting("active_node_id", str(nid))
    state.store.upsert_health(NodeHealth(node_id=nid, last_real_ok=True, last_real_ms=34,
                                         egress_ip="9.9.9.9"))
    with c.websocket_connect("/api/ws/traffic") as ws:
        frame = ws.receive_json()
    assert frame["outbounds"]["proxy"]["up_bps"] == 800.0
    assert frame["active"]["egress_ip"] == "9.9.9.9" and frame["active"]["real_ok"] is True
    assert "ts" in frame


def test_ws_disabled_frame_when_stats_off(settings, stub_xray):
    c, state = _client_state(settings, stub_xray)
    _auth(c)
    state.store.set_setting("stats_enabled", "0")
    with c.websocket_connect("/api/ws/traffic") as ws:
        assert ws.receive_json() == {"disabled": True}


class _BoomSampler:
    def sample(self):
        raise RuntimeError("stats unavailable")


def test_ws_sends_error_frame_on_sampler_failure(settings, stub_xray):
    c, state = _client_state(settings, stub_xray)
    _auth(c)
    state.recorder.record_error("stats unavailable")
    with c.websocket_connect("/api/ws/traffic") as ws:
        frame = ws.receive_json()
    assert "error" in frame and "stats unavailable" in frame["error"]


def test_established_ws_is_revoked_after_password_epoch_bump(settings, stub_xray):
    c, state = _client_state(settings, stub_xray)
    _auth(c)
    state.recorder.record_sample({"proxy": {"up_bps": 1.0, "down_bps": 2.0}})
    with c.websocket_connect("/api/ws/traffic") as ws:
        assert "outbounds" in ws.receive_json()
        state.store.set_setting("session_epoch", "1")
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 4401
