"""Failure-handling audit: failures that looked like success or ordinary data, store calls made on
the event loop, a drop-in rewritten in place, and a "hard" deadline that a drip-fed peer outlasts.

Every test here names a failure the panel used to hide. Where the fix is "don't block the event
loop", the probe is `_on_event_loop()` inside the store: SQL executed on a thread that is running
an event loop is exactly the stall `app.py` measured (4 ms → 1.7 s while an apply held the lock).
"""
import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
import urllib.request

import pytest

from conftest import _client, _login
from test_health_probe import _UNFINISHED, _bounded_call, _dribble, _loopback_http_server
from pi_gw_panel import diagnose
from pi_gw_panel.backup import backups_dir
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.devices import DeviceSampler
from pi_gw_panel.health import failover
from pi_gw_panel.health.liveness import LivenessLoop
from pi_gw_panel.health.monitor import HealthMonitor
from pi_gw_panel.models import Node, NodeHealth, Reservation
from pi_gw_panel.net_control import events, pd_client, plan, provision
from pi_gw_panel.net_control.linux import LinuxBackend
from pi_gw_panel.net_control.render import counter_names
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.stats.history import TrafficHistory, TrafficRecorder
from pi_gw_panel.subs.parsers import json_nodes


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _spy_sql_on_loop(monkeypatch, store) -> list[str]:
    """Record every statement the store executes on a thread that runs an event loop."""
    conn = store._conn
    real = conn.execute
    hits: list[str] = []

    def execute(sql, params=()):
        if _on_event_loop():
            hits.append(" ".join(sql.split()[:3]))
        return real(sql, params)

    monkeypatch.setattr(conn, "execute", execute)
    return hits


class _State:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.supervisor, self.net = None, None
        self.xray_bin = settings.xray_bin


def _state(settings) -> _State:
    conn = connect(settings.db_path, check_same_thread=False)
    init_schema(conn)
    return _State(NodeStore(conn), settings)


# --- store calls on the event loop ---------------------------------------------------------------

def test_the_traffic_tick_keeps_every_store_callback_off_the_event_loop():
    """The recorder's settings reads and its data-used flush reach the SQLite store every tick."""
    seen: list[tuple[str, bool]] = []

    def spy(name, value):
        def call(*_args):
            seen.append((name, _on_event_loop()))
            return value
        return call

    class Sampler:
        totals: dict = {}
        ticks = 0

        def sample(self):
            self.ticks += 1
            self.totals = {"proxy": {"up": 100 * self.ticks, "down": 200 * self.ticks}}
            return {"proxy": {"up_bps": 1.0, "down_bps": 2.0}}

    recorder = TrafficRecorder(
        sampler=Sampler(), history=TrafficHistory(), stats_enabled=spy("stats_enabled", True),
        running=spy("running", True), interval_ms=spy("interval_ms", 500),
        on_total=spy("on_total", None), flush_interval=0.0)

    async def two_ticks():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(recorder._run(), timeout=0.8)

    asyncio.run(two_ticks())
    assert {"stats_enabled", "running", "interval_ms", "on_total"} <= {name for name, _ in seen}
    assert [name for name, on_loop in seen if on_loop] == []


def test_stop_waits_for_an_in_flight_tick_before_the_final_flush():
    """With the tick on a worker thread, stop()'s cancel abandons a tick that is still running;
    the final flush must wait for it, or the bytes that tick counts are never persisted."""
    started, release = threading.Event(), threading.Event()
    flushed: list[tuple[int, int]] = []

    class Sampler:
        totals: dict = {}

        def sample(self):
            started.set()
            release.wait(2)
            self.totals = {"proxy": {"up": 300, "down": 700}}
            return {"proxy": {"up_bps": 1.0, "down_bps": 1.0}}

    recorder = TrafficRecorder(
        sampler=Sampler(), history=TrafficHistory(), stats_enabled=lambda: True,
        running=lambda: True, interval_ms=lambda: 500, clock=lambda: 10.0,
        on_total=lambda up, down: flushed.append((up, down)), flush_interval=3600.0,
        baseline={"up": 0, "down": 0})

    async def drive():
        recorder.start()
        assert await asyncio.to_thread(started.wait, 2)
        stopping = asyncio.create_task(recorder.stop())
        await asyncio.sleep(0.05)              # stop() has cancelled the loop; the tick runs on
        release.set()
        await stopping

    asyncio.run(drive())
    assert flushed == [(300, 700)]


def test_the_audit_middleware_keeps_its_store_calls_off_the_event_loop(settings, stub_xray, monkeypatch):
    c = _client(settings, stub_xray)
    token = _login(c)
    on_loop = _spy_sql_on_loop(monkeypatch, c.app.state.app_state.store)

    body = {"name": "n1", "address": "1.2.3.4", "port": 47000, "uuid": "u-1"}
    assert c.post("/api/nodes", json=body, headers={"X-CSRF-Token": token}).status_code == 200

    assert on_loop == []
    entry = next(e for e in c.get("/api/audit").json() if e["path"] == "/api/nodes")
    assert entry["actor"] == "user:admin" and entry["status"] == 200


def test_the_health_monitor_reads_its_interval_off_the_event_loop(settings, monkeypatch):
    st = _state(settings)
    st.store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u1"))
    on_loop = _spy_sql_on_loop(monkeypatch, st.store)
    ticked = threading.Event()

    async def drive():
        monitor = HealthMonitor(st, tcp_ping=lambda *_a, **_k: (True, 5),
                                http_ping=lambda *_a, **_k: (True, 6),
                                real_request=lambda *_: (True, 200, 9, "x"),
                                after_tick=ticked.set)            # no tick_sec: read from the store
        monitor.start()
        assert await asyncio.to_thread(ticked.wait, 2)
        await asyncio.sleep(0.1)                                  # the loop reads the interval next
        await monitor.stop()

    asyncio.run(drive())
    assert on_loop == []


def test_the_health_monitor_records_a_tick_that_could_not_run_off_the_event_loop(settings, monkeypatch):
    st = _state(settings)
    on_loop = _spy_sql_on_loop(monkeypatch, st.store)

    async def drive():
        monitor = HealthMonitor(st, tick_sec=0.05)
        monitor.start()
        monitor._executor.shutdown()                  # nothing can be scheduled: the error path
        await asyncio.sleep(0.2)
        await monitor.stop()

    asyncio.run(drive())
    assert any("could not run" in e["detail"] for e in st.store.list_events(limit=40))
    assert on_loop == []


def test_the_liveness_loop_records_a_tick_that_could_not_run_off_the_event_loop(settings, monkeypatch):
    st = _state(settings)
    on_loop = _spy_sql_on_loop(monkeypatch, st.store)

    async def drive():
        loop = LivenessLoop(st, interval_sec=0.05)
        loop.start()
        loop._executor.shutdown()
        await asyncio.sleep(0.2)
        await loop.stop()

    asyncio.run(drive())
    assert any("could not run" in e["detail"] for e in st.store.list_events(limit=40))
    assert on_loop == []


# --- device counters -----------------------------------------------------------------------------

TV_IP = "192.168.10.42"


class _DeviceStore:
    def __init__(self):
        self.rows: list[tuple] = []

    def list_reservations(self):
        return [Reservation(id=1, mac="aa:bb:cc:dd:ee:01", ip=TV_IP, name="tv")]

    def add_device_minute(self, ip, ts_min, up, down):
        self.rows.append((ip, ts_min, up, down))


def test_a_failed_counter_read_does_not_rebook_a_device_lifetime_bytes(settings):
    """One nft failure between two good readings used to reset the baseline to "no counters", so
    the next reading booked every byte since the table was loaded into a single minute."""
    up, down = counter_names(TV_IP)

    def reading(up_bytes, down_bytes):
        doc = {"nftables": [{"counter": {"name": up, "packets": 1, "bytes": up_bytes}},
                            {"counter": {"name": down, "packets": 1, "bytes": down_bytes}}]}
        return subprocess.CompletedProcess(["nft"], 0, stdout=json.dumps(doc), stderr="")

    replies = [reading(1_000, 4_000), None, reading(1_100, 4_400)]

    def run(cmd, **_kw):
        reply = replies.pop(0)
        if reply is None:
            raise subprocess.CalledProcessError(124, cmd, stderr="command timed out after 10s")
        return reply

    store = _DeviceStore()
    state = type("State", (), {"store": store, "net": LinuxBackend(settings, run=run)})()
    sampler = DeviceSampler(state)
    for minute in (10, 11, 12):
        sampler.sample_once(now=minute * 60)

    assert sum(row[2] for row in store.rows) == 1_100
    assert sum(row[3] for row in store.rows) == 4_400


# --- silent fallbacks now leave a trace ----------------------------------------------------------

def test_an_unreadable_reservation_table_is_logged(caplog):
    class Store:
        def list_reservations(self):
            raise sqlite3.OperationalError("database disk image is malformed")

    with caplog.at_level(logging.WARNING):
        assert plan._reservations(Store()) == []
    assert "reservation" in caplog.text


def test_a_reader_of_the_old_drop_in_never_sees_it_rewritten_in_place(tmp_path):
    path = tmp_path / "99-v2pi.conf"
    path.write_text("[keyfile]\nunmanaged-devices=interface-name:eth0.2\n")
    with open(path) as reader:
        provision._write_file(str(path), "[keyfile]\nunmanaged-devices=interface-name:eth0.3\n")
        assert reader.read() == "[keyfile]\nunmanaged-devices=interface-name:eth0.2\n"
    assert path.read_text() == "[keyfile]\nunmanaged-devices=interface-name:eth0.3\n"
    assert [p.name for p in tmp_path.iterdir()] == ["99-v2pi.conf"]


def test_a_drop_in_that_cannot_be_written_is_logged(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        provision._write_file(str(tmp_path / "missing-dir" / "99-v2pi.conf"), "[keyfile]\n")
    assert "99-v2pi.conf" in caplog.text


def test_removing_the_drop_in_is_quiet_when_absent_and_logged_when_it_fails(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        provision._remove_file(str(tmp_path / "absent.conf"))
    assert caplog.records == []
    stuck = tmp_path / "stuck.conf"
    stuck.mkdir()                                  # os.remove refuses a directory: a real failure
    with caplog.at_level(logging.WARNING):
        provision._remove_file(str(stuck))
    assert "stuck.conf" in caplog.text


def test_a_refused_networkmanager_reload_is_logged(caplog):
    def run(cmd, **_kw):
        raise subprocess.CalledProcessError(1, cmd, stderr="Error: NetworkManager is not running.")

    with caplog.at_level(logging.WARNING):
        provision._nm_reload(run, nm_active=lambda: True)
    assert "reload" in caplog.text


def test_a_backups_directory_that_cannot_be_locked_down_is_logged(settings, monkeypatch, caplog):
    def refuse(path, mode):
        raise PermissionError(1, "Operation not permitted", path)

    monkeypatch.setattr(os, "chmod", refuse)
    with caplog.at_level(logging.WARNING):
        path = backups_dir(settings)
    assert path == os.path.join(settings.data_dir, "backups")
    assert path in caplog.text


def test_lost_and_unreadable_connection_events_are_logged(caplog):
    class Store:
        def add_event(self, *_args):
            raise sqlite3.OperationalError("disk I/O error")

        def list_events(self, **_kwargs):
            raise sqlite3.OperationalError("disk I/O error")

    with caplog.at_level(logging.WARNING):
        events.record(Store(), "connect", "node 1")            # still never raises
        assert events.recent(Store()) == []
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 2


def test_an_uncountable_failover_total_is_null_not_zero(settings, stub_xray, monkeypatch, caplog):
    c = _client(settings, stub_xray)
    _login(c)

    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("no such table: conn_events")

    monkeypatch.setattr(c.app.state.app_state.store, "count_events", broken)
    with caplog.at_level(logging.WARNING):
        body = c.get("/api/status").json()
    assert body["failovers_24h"] is None
    assert "failover" in caplog.text


def test_an_unreadable_prefix_file_keeps_the_last_delegation(tmp_path, caplog):
    seen = []
    client = pd_client.PdClient("eth0", str(tmp_path / "hook.sh"), on_prefix_change=seen.append)
    prefix_file = tmp_path / "hook.sh.prefix"
    prefix_file.write_text("2001:db8:1200::/56\n")
    client.poll_once()

    prefix_file.unlink()
    prefix_file.mkdir()                            # there, but unreadable: not an expiry
    with caplog.at_level(logging.WARNING):
        client.poll_once()
    assert seen == ["2001:db8:1200::/56"]
    assert "prefix" in caplog.text

    prefix_file.rmdir()                            # the hook's `rm -f`: the delegation is gone
    client.poll_once()
    assert seen == ["2001:db8:1200::/56", None]


def test_an_unreadable_client_roster_reports_itself(settings, stub_xray, caplog):
    c = _client(settings, stub_xray)
    _login(c)
    c.app.state.app_state.store.set_setting("rw_clients", "{not json")
    with caplog.at_level(logging.WARNING):
        body = c.get("/api/rw").json()
    assert body["clients"] == []
    assert "client" in body["state_error"]
    assert "client" in caplog.text


# --- JSON feeds ----------------------------------------------------------------------------------

def test_json_feed_entries_the_panel_cannot_use_are_skipped_and_counted():
    """An unsupported protocol used to become VLESS, and a missing credential a node that fails
    backup validation — which then blocked every backup and every restore."""
    skipped: dict = {}
    nodes = json_nodes.parse_obj({"nodes": [
        {"address": "a.example", "port": 443, "uuid": "u-a", "protocol": "vless"},
        {"address": "b.example", "port": 443, "uuid": "u-b", "protocol": "vmess"},
        {"address": "c.example", "port": 443},
        {"address": "d.example", "port": 443, "protocol": "trojan"},
        {"address": "e.example", "port": 443, "protocol": "shadowsocks", "password": "p",
         "method": "rc4-md5"},
        {"address": "f.example", "port": 443, "protocol": "Trojan", "password": "secret"},
    ]}, skipped=skipped)
    assert [(n.address, n.protocol) for n in nodes] == [("a.example", "vless"), ("f.example", "trojan")]
    assert skipped == {"vmess": 1, "invalid": 3}


# --- failover preflight --------------------------------------------------------------------------

def _failover_setup(settings):
    st = _state(settings)
    active, first, second = [
        st.store.add_node(Node(id=None, name=f"n{i}", address=f"{i}.{i}.{i}.{i}", port=443,
                               uuid=f"u{i}"))
        for i in (1, 2, 3)]
    st.store.set_setting("active_node_id", str(active))
    checked = "1970-01-01T00:16:40+00:00"                        # now = 1000
    st.store.upsert_health(NodeHealth(node_id=active, last_tcp_ok=True, fail_count=3, checked_at=checked))
    st.store.upsert_health(NodeHealth(node_id=first, last_http_ok=True, last_http_ms=5, checked_at=checked))
    st.store.upsert_health(NodeHealth(node_id=second, last_tcp_ok=True, last_tcp_ms=10, checked_at=checked))
    return st, first, second


def _apply(node, *_args, store=None, **_kwargs):
    from pi_gw_panel.controller import ApplyResult
    store.set_setting("active_node_id", str(node.id))
    return ApplyResult(ok=True)


def test_a_preflight_that_raises_is_logged_and_not_booked_as_a_node_failure(settings, caplog):
    st, first, second = _failover_setup(settings)

    def real_through(node, *_args, **_kwargs):
        if node.id == first:
            raise OSError(13, "Permission denied", "/usr/local/bin/xray")
        return True, 12, "203.0.113.9", None

    with caplog.at_level(logging.WARNING):
        assert failover.run(st, 1000.0, apply_fn=_apply, real_through=real_through) == second
    untouched = st.store.get_health(first)
    assert untouched.fail_count == 0 and untouched.last_real_ok is None
    assert "Permission denied" in caplog.text


def test_failover_raises_when_no_preflight_could_run(settings):
    """Every candidate raising is our failure, not theirs: it must reach the liveness loop's
    "failover evaluation failed" event instead of reading as "all nodes down"."""
    st, _first, _second = _failover_setup(settings)

    def real_through(*_args, **_kwargs):
        raise OSError(2, "No such file or directory", "/usr/local/bin/xray")

    with pytest.raises(RuntimeError, match="preflight"):
        failover.run(st, 1000.0, apply_fn=_apply, real_through=real_through)
    assert all(e["kind"] != "all-nodes-down" for e in st.store.list_events(limit=40))


# --- diagnose deadline ---------------------------------------------------------------------------

def test_a_drip_fed_transfer_stops_at_the_wall_clock_deadline(monkeypatch):
    """Throttling looks exactly like this from the inside, and urllib's timeout is only an idle
    timer: without a guard one 32 KiB read outlasts the 30 s "hard" bound by minutes."""
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    srv, port = _loopback_http_server(
        lambda conn: _dribble(conn, b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n"))
    try:
        result, elapsed = _bounded_call(
            lambda: diagnose._transfer(f"http://127.0.0.1:{port}", "http://diag.invalid/file",
                                       100_000, time.monotonic() + 1.0), 5.0)
    finally:
        srv.close()
    assert result is not _UNFINISHED, "the transfer never returned — the deadline is not hard"
    assert elapsed < 3.0
    assert result["stalled"] is True and result["error"] == ""


# --- route test ----------------------------------------------------------------------------------

def test_the_route_test_refuses_a_port_nobody_typed(settings, stub_xray):
    c = _client(settings, stub_xray)
    headers = {"X-CSRF-Token": _login(c)}
    for destination in ("example.com:80x", "example.com:70000", "example.com:0"):
        body = c.post("/api/routing/test", json={"destination": destination}, headers=headers).json()
        assert body["ok"] is False and "port" in body["error"], destination
