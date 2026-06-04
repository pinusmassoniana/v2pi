"""Backend coverage for the 2026-06-04 dashboard audit (v1.6).

D2 — traffic WS honours the FULL session contract (epoch + idle timeout), not just authed.
D4 — /status carries the server wall-clock for client clock-skew correction.
D6 — /nodes/{id}/probe?real_only=1 skips the two direct probes, refreshes only the real result.
B  — active-node health snapshot / WS frame carry lat_history for the dashboard sparkline.
F  — the recorder accumulates a durable "data used" total (batched, reset-safe); WS frame
     exposes it as `lifetime`.
"""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.models import Node, NodeHealth
from pi_gw_panel.stats.history import TrafficHistory, TrafficRecorder
from pi_gw_panel.api.deps import session_invalid_reason
from pi_gw_panel.auth.auth import SESSION_AUTHED, SESSION_EPOCH, SESSION_LASTSEEN
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.health import probe as probe_mod


def _client_state(settings, stub_xray):
    settings.xray_bin = stub_xray
    state = build_state(settings, net=DryRunBackend())
    return TestClient(create_app(settings, state=state)), state


def _auth(c):
    c.post("/api/setup", json={"username": "admin", "password": "changeme"})
    return c.get("/api/csrf").json()["csrf"]


def _store():
    conn = connect(":memory:")
    init_schema(conn)
    return NodeStore(conn)


class _FakeSampler:
    def __init__(self):
        self.totals = {"proxy": {"up": 10, "down": 20}}

    def sample(self):
        return {"proxy": {"up_bps": 800.0, "down_bps": 1600.0}}


# --- D2: WS session contract ------------------------------------------------

def test_ws_rejects_after_password_epoch_bump(settings, stub_xray):
    c, state = _client_state(settings, stub_xray)
    _auth(c)
    # a password change bumps session_epoch; the existing cookie's epoch is now stale
    state.store.set_setting("session_epoch", "1")
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/api/ws/traffic"):
            pass


def test_session_invalid_reason_enforces_epoch_and_idle():
    store = _store()
    good = {SESSION_AUTHED: True, SESSION_EPOCH: 0, SESSION_LASTSEEN: 10**12}
    assert session_invalid_reason(good, store) is None
    assert session_invalid_reason({}, store) == "auth required"
    assert session_invalid_reason({SESSION_AUTHED: True, SESSION_EPOCH: 5}, store) == "session expired"
    store.set_setting("session_timeout_min", "1")
    stale = {SESSION_AUTHED: True, SESSION_EPOCH: 0, SESSION_LASTSEEN: 0}   # last seen at epoch 0
    assert session_invalid_reason(stale, store) == "session idle timeout"


# --- D4: server clock in /status -------------------------------------------

def test_status_carries_server_now(settings, stub_xray):
    import time
    c, _ = _client_state(settings, stub_xray)
    _auth(c)
    now = c.get("/api/status").json()["server_now"]
    assert abs(now - time.time()) < 5


# --- D6: real_only probe ----------------------------------------------------

def test_probe_real_only_skips_direct_probes(settings, stub_xray, monkeypatch):
    c, state = _client_state(settings, stub_xray)
    tok = _auth(c)
    h = {"X-CSRF-Token": tok}
    nid = c.post("/api/nodes", json={"name": "n", "address": "1.2.3.4", "port": 443, "uuid": "u-1"},
                 headers=h).json()["id"]

    calls = []
    monkeypatch.setattr(probe_mod, "tcp_ping", lambda *a, **k: (calls.append("tcp"), (True, 10))[1])
    monkeypatch.setattr(probe_mod, "http_ping", lambda *a, **k: (calls.append("http"), (True, 20))[1])
    monkeypatch.setattr(probe_mod, "real_through_node", lambda *a, **k: (True, 42, "5.5.5.5", None))

    out = c.post(f"/api/nodes/{nid}/probe?real_only=1", headers=h).json()
    assert calls == []                                  # neither direct probe ran (D6)
    assert out["last_real_ms"] == 42 and out["egress_ip"] == "5.5.5.5"
    assert out["last_tcp_ok"] is None and out["last_http_ok"] is None
    assert out["lat_history"] == [42]                   # real latency feeds the sparkline (B)

    out2 = c.post(f"/api/nodes/{nid}/probe", headers=h).json()   # full sweep
    assert set(calls) == {"tcp", "http"}                # both direct probes ran
    assert out2["last_tcp_ok"] is True and out2["last_http_ok"] is True
    assert out2["lat_history"] == [42, 20]              # full sweep records the http latency


# --- B + F: WS frame carries lat_history and durable lifetime ---------------

def test_ws_frame_has_lifetime_and_lat_history(settings, stub_xray):
    c, state = _client_state(settings, stub_xray)
    _auth(c)
    state.sampler = _FakeSampler()
    state.store.set_setting("data_used_up", "111")
    state.store.set_setting("data_used_down", "222")
    nid = state.store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u"))
    state.store.set_setting("active_node_id", str(nid))
    state.store.upsert_health(NodeHealth(node_id=nid, last_real_ok=True, last_real_ms=34,
                                         egress_ip="9.9.9.9"))
    state.store.record_latency(nid, 30)   # lat_history is owned by record_latency, not upsert
    state.store.record_latency(nid, 34)
    with c.websocket_connect("/api/ws/traffic") as ws:
        frame = ws.receive_json()
    assert frame["lifetime"] == {"up": 111, "down": 222}      # F: survives restart
    assert frame["active"]["lat_history"] == [30, 34]         # B: sparkline source


# --- F: recorder accumulation (batched + reset-safe) ------------------------

class _TotalsSampler:
    def __init__(self):
        self.totals = {}

    def sample(self):
        return {}


def test_recorder_accumulates_data_used_batched_and_reset_safe():
    acc = {"up": 0, "down": 0}

    def on_total(u, d):
        acc["up"] += u
        acc["down"] += d

    clk = {"t": 0.0}
    s = _TotalsSampler()
    r = TrafficRecorder(sampler=s, history=TrafficHistory(), stats_enabled=lambda: True,
                        interval_ms=lambda: 1000, clock=lambda: clk["t"],
                        on_total=on_total, flush_interval=30.0)

    s.totals = {"proxy": {"up": 1000, "down": 2000}}
    r.record_sample({})
    assert acc == {"up": 0, "down": 0}                  # baseline, no prior counters → no delta

    clk["t"] = 5.0
    s.totals = {"proxy": {"up": 1500, "down": 3000}}
    r.record_sample({})
    assert acc == {"up": 0, "down": 0}                  # within flush window → still pending

    clk["t"] = 40.0
    s.totals = {"proxy": {"up": 1600, "down": 3100}}
    r.record_sample({})
    assert acc == {"up": 600, "down": 1100}             # flushed: (500+100)/(1000+100)

    clk["t"] = 80.0
    s.totals = {"proxy": {"up": 50, "down": 60}}        # counter reset (xray restart)
    r.record_sample({})
    assert acc == {"up": 650, "down": 1160}             # reset counted from 0, not negative
