import time
from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.stats.history import TrafficHistory, TrafficRecorder, _downsample


def test_history_record_window_and_maxlen():
    h = TrafficHistory(maxlen=5)
    for i in range(7):
        h.record(1000 + i * 1000, i * 10, i * 20)
    assert len(h) == 5                        # oldest two dropped by the ring buffer
    s = h.series()
    assert s[0][0] == 3000                     # first kept = 3rd inserted
    assert s[-1] == (7000, 60, 120)
    win = h.series(since_ms=5000)              # time-window filter
    assert win[0][0] == 5000 and all(x[0] >= 5000 for x in win)


def test_history_downsample_keeps_last():
    h = TrafficHistory(maxlen=200)
    for i in range(200):
        h.record(i, i, i)
    s = h.series(max_points=10)
    assert len(s) == 10
    assert s[-1] == (199, 199, 199)            # most-recent always kept


def test_downsample_noop_when_small():
    assert _downsample([1, 2, 3], 10) == [1, 2, 3]


def test_recorder_maps_proxy_sample():
    h = TrafficHistory(maxlen=10)
    clk = iter([5.0, 6.0])
    r = TrafficRecorder(sampler=None, history=h, stats_enabled=lambda: True,
                        interval_ms=lambda: 1000, clock=lambda: next(clk))
    r.record_sample({"proxy": {"up_bps": 800.0, "down_bps": 1600.0},
                     "direct": {"up_bps": 1.0, "down_bps": 2.0}})
    assert h.series()[-1] == (5000, 800, 1600)  # only the proxy outbound is recorded
    r.record_sample({})                         # no proxy → zeros, never raises
    assert h.series()[-1] == (6000, 0, 0)


def test_traffic_history_endpoint(settings, stub_xray):
    settings.xray_bin = stub_xray
    state = build_state(settings, net=DryRunBackend())
    c = TestClient(create_app(settings, state=state))
    c.post("/api/setup", json={"username": "admin", "password": "changeme"})
    now = int(time.time() * 1000)
    for i in range(5):
        state.history.record(now - (5 - i) * 1000, i * 100, i * 200)
    r = c.get("/api/traffic/history?window_sec=3600&max_points=600")
    assert r.status_code == 200
    body = r.json()
    assert body["interval_ms"] == 1000
    assert len(body["samples"]) == 5
    assert body["samples"][-1][1:] == [400, 800]


def test_traffic_history_requires_auth(settings, stub_xray):
    settings.xray_bin = stub_xray
    state = build_state(settings, net=DryRunBackend())
    c = TestClient(create_app(settings, state=state))
    assert c.get("/api/traffic/history").status_code == 401
