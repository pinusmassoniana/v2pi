import asyncio
import time
import pytest
from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.stats.history import TrafficHistory, TrafficRecorder, _downsample
from pi_gw_panel.app import _traffic_frame


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


def test_recorder_snapshot_is_immutable_and_retains_error_as_stale():
    class Sampler:
        totals = {"proxy": {"up": 10, "down": 20}}

    recorder = TrafficRecorder(
        sampler=Sampler(), history=TrafficHistory(), stats_enabled=lambda: True,
        interval_ms=lambda: 1000, clock=lambda: 5.0)
    recorder.record_sample({"proxy": {"up_bps": 8.0, "down_bps": 16.0}})
    first = recorder.snapshot()
    first["outbounds"]["proxy"]["up_bps"] = 999
    assert recorder.snapshot()["outbounds"]["proxy"]["up_bps"] == 8.0
    recorder.record_error("rpc gap")
    stale = recorder.snapshot()
    assert stale["stale"] is True and stale["error"] == "rpc gap"
    assert stale["outbounds"]["proxy"]["down_bps"] == 16.0


def test_minute_flush_failure_retries_without_double_counting():
    class Sampler:
        totals = {}

    sampler = Sampler()
    now = {"value": 0.0}
    saved = []
    attempts = {"count": 0}

    def persist(minute, up, down):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("transient sqlite error")
        saved.append((minute, up, down))

    recorder = TrafficRecorder(
        sampler=sampler, history=TrafficHistory(), stats_enabled=lambda: True,
        interval_ms=lambda: 1000, clock=lambda: now["value"], on_minute=persist)
    for at, total in ((0.0, 100), (10.0, 150), (70.0, 200), (80.0, 260)):
        now["value"] = at
        sampler.totals = {"proxy": {"up": total, "down": 0}}
        recorder.record_sample({})
    recorder.flush_minute(include_current=True)
    assert saved == [(0, 50, 0), (1, 110, 0)]
    assert sum(up for _, up, _ in saved) == 160


def test_lifetime_total_callback_rolls_back_both_counters_on_partial_failure(
        settings, monkeypatch):
    state = build_state(settings, net=DryRunBackend())
    store = state.store
    store.set_setting("data_used_up", "10")
    store.set_setting("data_used_down", "20")
    original = store.set_setting
    fail_once = {"value": True}

    def flaky_set(key, value):
        if key == "data_used_down" and fail_once["value"]:
            fail_once["value"] = False
            raise RuntimeError("simulated second-write failure")
        original(key, value)

    monkeypatch.setattr(store, "set_setting", flaky_set)
    with pytest.raises(RuntimeError, match="second-write"):
        state.recorder._on_total(5, 7)
    assert store.get_setting("data_used_up") == "10"
    assert store.get_setting("data_used_down") == "20"

    state.recorder._on_total(5, 7)
    assert store.get_setting("data_used_up") == "15"
    assert store.get_setting("data_used_down") == "27"
    state.close()


def test_recorder_survives_invalid_interval_value(settings):
    state = build_state(settings, net=DryRunBackend())
    state.store.set_setting("traffic_sample_ms", "not-an-integer")
    recorder = state.recorder

    async def run_briefly():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(recorder._run(), timeout=0.02)

    asyncio.run(run_briefly())
    state.close()


def test_recorder_marks_last_sample_stale_while_xray_is_stopped():
    class Sampler:
        totals = {}

    recorder = TrafficRecorder(
        sampler=Sampler(), history=TrafficHistory(), stats_enabled=lambda: True,
        running=lambda: False, interval_ms=lambda: 500)
    recorder.record_sample({"proxy": {"up_bps": 1.0, "down_bps": 2.0}})

    async def run_briefly():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(recorder._run(), timeout=0.02)

    asyncio.run(run_briefly())
    snapshot = recorder.snapshot()
    assert snapshot["stale"] is True
    assert snapshot["error"]


def test_traffic_frame_tolerates_missing_stats_client(settings):
    state = build_state(settings, net=DryRunBackend())

    class LegacyState:
        store = state.store
        recorder = state.recorder

    frame = _traffic_frame(LegacyState())
    assert frame["stats"]["last_error"] == "stats client unavailable"
    state.close()


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
