"""Durability/secret-mode regressions: traffic retention, the stats recorder's failure
paths, the session secret and app log file modes, and the store's transaction contract.

Every test here must fail if the defect it pins is reintroduced.
"""
import logging
import os
import stat
import threading

import pytest

from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.logs import setup_app_logging, teardown_app_logging
from pi_gw_panel.models import Node, NodeHealth
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.nodes.store import NodeStore, TransactionRolledBack
from pi_gw_panel.state import build_state
from pi_gw_panel.stats.history import TrafficHistory, TrafficRecorder
from pi_gw_panel.__main__ import ensure_session_secret


def _store(settings) -> NodeStore:
    conn = connect(settings.db_path, check_same_thread=False)
    init_schema(conn)
    return NodeStore(conn)


class _Sampler:
    """Stand-in for TrafficSampler: only `totals` is read by the recorder."""
    def __init__(self, totals=None):
        self.totals = totals or {}

    def sample(self):
        return {}


def _recorder(**kw):
    kw.setdefault("sampler", _Sampler())
    kw.setdefault("history", TrafficHistory(maxlen=100))
    kw.setdefault("stats_enabled", lambda: True)
    kw.setdefault("interval_ms", lambda: 1000)
    return TrafficRecorder(**kw)


# --- F2-4: a clock step must not wipe the durable traffic history ---

def test_far_future_sample_does_not_wipe_traffic_history(settings):
    store = _store(settings)
    store.add_traffic_minute(1000, 10, 1)
    store.add_traffic_minute(1001, 20, 2)
    # One NTP/RTC step forward. The prune floor used to be derived from this sample alone,
    # so a single bad timestamp deleted 90 days of history in one call.
    store.add_traffic_minute(1001 + store._TRAFFIC_RETENTION_MIN * 5, 1, 1)
    kept = {r["ts_min"] for r in store.traffic_minutes(since_min=0)}
    assert {1000, 1001} <= kept, f"history wiped by a far-future sample: {sorted(kept)}"


def test_traffic_retention_still_prunes_once_the_series_advances(settings):
    store = _store(settings)
    span = store._TRAFFIC_RETENTION_MIN
    store.add_traffic_minute(1000, 5, 5)
    store.add_traffic_minute(1000 + span + 1, 1, 1)     # the jump itself prunes nothing
    assert 1000 in {r["ts_min"] for r in store.traffic_minutes(since_min=0)}
    store.add_traffic_minute(1000 + span + 2, 1, 1)     # now the series really is past the window
    kept = {r["ts_min"] for r in store.traffic_minutes(since_min=0)}
    assert 1000 not in kept and 1000 + span + 1 in kept


# --- F8-1/F8-2: a failing writer must not grow unbounded, and must say so audibly ---

def test_pending_minutes_is_bounded_and_the_drop_is_logged(caplog):
    now = {"t": 0.0}
    sampler = _Sampler()

    def persist(*_args):
        raise RuntimeError("database is locked")

    rec = _recorder(sampler=sampler, clock=lambda: now["t"], on_minute=persist)
    caplog.set_level(logging.WARNING, logger="pi_gw_panel")
    total = 0
    for minute in range(rec._MAX_PENDING_MINUTES + 40):
        now["t"] = minute * 60.0
        total += 100
        sampler.totals = {"proxy": {"up": total, "down": 0}}
        rec.record_sample({})
    assert len(rec._pending_minutes) == rec._MAX_PENDING_MINUTES
    assert any("backlog full" in r.message for r in caplog.records if r.levelno >= logging.WARNING)


def test_recorder_failures_reach_the_app_log_file(tmp_path):
    """The failures that drive the backlog must be visible in the log the panel ships,
    whose root level is INFO — `log.debug` there means no trail at all."""
    directory = tmp_path / "logs"
    path = directory / "app.log"
    now = {"t": 0.0}
    sampler = _Sampler()
    rec = _recorder(sampler=sampler, clock=lambda: now["t"],
                    on_minute=lambda *_a: (_ for _ in ()).throw(RuntimeError("db down")))
    handler = setup_app_logging(str(path))
    try:
        for at, total in ((0.0, 100), (10.0, 200), (70.0, 300)):
            now["t"] = at
            sampler.totals = {"proxy": {"up": total, "down": 0}}
            rec.record_sample({})
        handler.flush()
    finally:
        teardown_app_logging(handler)
    assert "traffic-minute flush failed" in path.read_text()


# --- F8-5: the app log is as private as every other artifact the panel writes ---

def test_app_log_and_dir_are_owner_only_across_a_rollover(tmp_path, monkeypatch):
    directory = tmp_path / "logs"
    path = directory / "app.log"
    # As with the session secret, the modes have to come from the create calls themselves —
    # the chmod afterwards only repairs artifacts that a previous run left behind.
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    previous = os.umask(0)
    try:
        handler = setup_app_logging(str(path))
    finally:
        os.umask(previous)
    try:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
        previous = os.umask(0)
        try:
            handler.doRollover()          # the rotated-in generation must be private too
        finally:
            os.umask(previous)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    finally:
        teardown_app_logging(handler)


def test_app_log_left_world_readable_by_an_earlier_run_is_tightened(tmp_path):
    directory = tmp_path / "logs"
    directory.mkdir()
    path = directory / "app.log"
    path.write_text("from a previous version\n")
    os.chmod(path, 0o644)
    os.chmod(directory, 0o755)
    handler = setup_app_logging(str(path))
    try:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    finally:
        teardown_app_logging(handler)


# --- F2-9: the session secret is created 0600, not chmod'd to it afterwards ---

def test_session_secret_is_created_owner_only(tmp_path, monkeypatch):
    data = str(tmp_path / "data")
    # Neutralise the post-hoc chmod: the mode has to come from the create itself, or the
    # secret that forges every session cookie is world-readable for the window in between.
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    previous = os.umask(0)
    try:
        secret = ensure_session_secret(data)
    finally:
        os.umask(previous)
    path = os.path.join(data, "session_secret")
    assert len(secret) >= 32
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(data).st_mode) == 0o700


def test_backups_dir_is_created_owner_only(settings, monkeypatch):
    from pi_gw_panel.backup import backups_dir
    # Same reasoning as the session secret: the backups hold subscription URLs, so the mode
    # must be on the create — the chmod that follows only repairs an already-open directory.
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    previous = os.umask(0)
    try:
        path = backups_dir(settings)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o700


def test_session_secret_survives_a_short_leftover_file(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "session_secret").write_text("too-short")
    secret = ensure_session_secret(str(data))
    assert len(secret) >= 32
    assert (data / "session_secret").read_text() == secret


# --- F2-5: a rolled-back unit of work must never return normally ---

def test_swallowed_nested_failure_does_not_report_success(settings):
    store = _store(settings)
    with pytest.raises(TransactionRolledBack):
        with store.transaction():
            store.set_setting("outer_key", "1")
            try:
                with store.transaction():
                    store.set_setting("inner_key", "2")
                    raise RuntimeError("inner step failed")
            except RuntimeError:
                pass                      # caller "handles" it — the writes are gone anyway
    assert store.get_setting("outer_key") is None
    assert store.get_setting("inner_key") is None


def test_clean_nested_transaction_still_commits(settings):
    store = _store(settings)
    with store.transaction():
        store.set_setting("outer_key", "1")
        with store.transaction():
            store.set_setting("inner_key", "2")
    assert (store.get_setting("outer_key"), store.get_setting("inner_key")) == ("1", "2")


# --- F2-6: a corrupt latency ring costs one node its history, not the whole sweep ---

def test_corrupt_lat_history_does_not_abort_the_caller(settings):
    store = _store(settings)
    nid = store.add_node(Node(id=None, name="n", address="1.1.1.1", port=443, uuid="u"))
    store.upsert_health(NodeHealth(node_id=nid, last_tcp_ok=True))
    store.record_latency(nid, 5)
    store._conn.execute("UPDATE node_health SET lat_history = ? WHERE node_id = ?",
                        ("{not json", nid))
    store.record_latency(nid, 7)                      # must not raise
    assert store.get_health(nid).lat_history == [7]


# --- F2-8: two concurrent revocations must not both report (and audit) success ---

def test_concurrent_delete_token_reports_exactly_one_success(settings):
    store = _store(settings)
    row = store.create_token("t", "readwrite", "h" * 64, "pfx")
    barrier = threading.Barrier(2)
    real_execute = store._conn.execute

    def execute(sql, params=()):
        result = real_execute(sql, params)
        if sql.startswith("SELECT 1 FROM api_tokens"):
            try:
                # Hold each probe until the other has run: both see the row unless the
                # DELETE that follows is inside the same transaction (which serialises them,
                # so the second thread never reaches this point and the barrier times out).
                barrier.wait(timeout=0.5)
            except threading.BrokenBarrierError:
                pass
        return result

    store._conn.execute = execute
    results = []
    lock = threading.Lock()

    def revoke():
        outcome = store.delete_token(row["id"])
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=revoke) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert sorted(results) == [False, True], f"both revocations claimed success: {results}"


# --- F8-3: the absolute counters survive a restart, so no interval is dropped ---

def test_restored_baseline_counts_the_downtime_in_the_total_only():
    totals, minutes, baselines = [], [], []
    rec = _recorder(sampler=_Sampler({"proxy": {"up": 5_000, "down": 500}}),
                    clock=lambda: 600.0, flush_interval=0.0,
                    baseline={"up": 1_000, "down": 100},
                    on_total=lambda u, d: totals.append((u, d)),
                    on_minute=lambda m, u, d: minutes.append((m, u, d)),
                    on_baseline=lambda u, d: baselines.append((u, d)))
    rec.record_sample({})
    rec.flush_minute(include_current=True)
    assert totals == [(4_000, 400)]      # the whole downtime gap, counted once
    assert minutes == []                 # ...and not drawn as a spike in the minute it resumed
    assert baselines[-1] == (5_000, 500)


def test_data_used_survives_a_panel_restart(settings):
    state = build_state(settings, net=DryRunBackend())
    state.recorder._sampler = _Sampler({"proxy": {"up": 1_000, "down": 100}})
    state.recorder.record_sample({})                 # first tick: baseline only
    state.recorder._sampler.totals = {"proxy": {"up": 3_000, "down": 300}}
    state.recorder.record_sample({})                 # +2000/+200 counted and flushed
    assert state.store.get_setting("data_used_up") == "2000"
    assert state.store.get_setting("proxy_abs_baseline") == "3000:300"
    state.close()

    restarted = build_state(settings, net=DryRunBackend())
    restarted.recorder._sampler = _Sampler({"proxy": {"up": 9_000, "down": 900}})
    restarted.recorder.record_sample({})             # the panel was down for 6000/600 bytes
    assert restarted.store.get_setting("data_used_up") == "8000"
    assert restarted.store.get_setting("data_used_down") == "800"
    restarted.close()


def test_flush_total_rolls_back_the_baseline_when_the_total_write_fails(settings, monkeypatch):
    """FIX-E-1: baseline and total must commit or rollback TOGETHER. The earlier fix wrote the
    restart baseline (`proxy_abs_baseline`) in its own transaction, before the lifetime-byte
    delta in a separate one — a callback failure (or crash) landing between the two let the
    baseline commit durably while the bytes it covers were never added, losing that delta
    forever. With both writes sharing one store transaction, a failure anywhere rolls back the
    whole flush, so the pending delta survives for retry and nothing is ever lost."""
    state = build_state(settings, net=DryRunBackend())
    store = state.store
    recorder = state.recorder
    store.set_setting("proxy_abs_baseline", "1000:100")
    recorder._prev_abs = {"up": 4_000, "down": 400}        # advanced in memory, not yet persisted
    recorder._pending = {"up": 3_000, "down": 300}         # the delta the new baseline covers

    original = store.set_setting

    def flaky_set(key, value):
        if key == "data_used_up":
            raise RuntimeError("simulated failure between baseline and total writes")
        original(key, value)

    monkeypatch.setattr(store, "set_setting", flaky_set)
    with pytest.raises(RuntimeError, match="simulated failure"):
        recorder.flush_total()

    # Rolled back TOGETHER: the baseline must not have advanced without the bytes it covers.
    assert store.get_setting("proxy_abs_baseline") == "1000:100"
    assert store.get_setting("data_used_up") is None
    assert recorder._pending == {"up": 3_000, "down": 300}          # preserved for retry

    monkeypatch.setattr(store, "set_setting", original)
    recorder.flush_total()                                          # retry succeeds atomically
    assert store.get_setting("proxy_abs_baseline") == "4000:400"
    assert store.get_setting("data_used_up") == "3000"
    assert store.get_setting("data_used_down") == "300"
    state.close()


# --- F8-4: byte accounting buckets on the same timestamp the series shows ---

def test_backward_clock_step_buckets_on_the_clamped_timestamp():
    now = {"t": 600.0}
    sampler = _Sampler()
    minutes = []
    rec = _recorder(sampler=sampler, clock=lambda: now["t"],
                    on_minute=lambda m, u, d: minutes.append((m, u, d)))
    for at, total in ((600.0, 0), (610.0, 100)):
        now["t"] = at
        sampler.totals = {"proxy": {"up": total, "down": 0}}
        rec.record_sample({})
    now["t"] = 60.0                                  # NTP steps the clock back nine minutes
    sampler.totals = {"proxy": {"up": 150, "down": 0}}
    rec.record_sample({})
    rec.flush_minute(include_current=True)
    assert minutes == [(10, 150, 0)]                 # one bucket, matching the clamped series
