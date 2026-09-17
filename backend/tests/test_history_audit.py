"""A9 + A7: how often did the tunnel drop, and how much has this gateway moved?

Neither question could be answered before: events lived in a 40-entry ring in the settings k/v,
and the traffic history was only ever served as a chart the browser had to add up itself.
"""
import time

import pytest

from conftest import _client, _login
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.net_control import events as conn_events
from pi_gw_panel.nodes.store import NodeStore


@pytest.fixture()
def store(tmp_path):
    conn = connect(str(tmp_path / "t.db"), check_same_thread=False)
    init_schema(conn)
    return NodeStore(conn)


# --- the log itself ---

def test_events_are_rows_now_and_recent_still_reads_oldest_to_newest(store):
    for index in range(50):
        conn_events.record(store, "connect", f"n{index}", now=1_000 + index)

    recent = conn_events.recent(store)
    assert len(recent) == 40                       # the Network card's feed is unchanged …
    assert recent[0]["detail"] == "n10" and recent[-1]["detail"] == "n49"   # … oldest first
    # … and the history behind it is no longer capped at what the card shows.
    assert len(store.list_events(limit=1000)) == 50
    assert store.count_events("connect", since=1_040) == 10


def test_recording_never_raises_because_telemetry_may_not_break_a_connection(store):
    class _Broken:
        def add_event(self, *_a, **_k):
            raise RuntimeError("disk full")

    conn_events.record(_Broken(), "connect", "x")   # must not propagate
    assert conn_events.recent(_Broken()) == []


def test_the_old_ring_is_carried_over_by_the_migration(tmp_path):
    """An upgrade must not lose the events the operator can see right now."""
    import json
    conn = connect(str(tmp_path / "u.db"), check_same_thread=False)
    init_schema(conn)
    conn.execute("DELETE FROM conn_events")
    conn.execute("INSERT INTO settings(key,value) VALUES('conn_events',?)",
                 (json.dumps([{"ts": 10, "kind": "connect", "detail": "old"},
                              {"ts": 20, "kind": "failover", "detail": "older feed"}]),))
    conn.execute("PRAGMA user_version = 19")       # pretend 20 has not run
    conn.commit()

    init_schema(conn)                              # applies migration 20

    rows = NodeStore(conn).list_events(limit=10)
    assert [row["detail"] for row in rows] == ["old", "older feed"]
    assert conn.execute("SELECT 1 FROM settings WHERE key='conn_events'").fetchone() is None


def test_retention_prunes_by_age_but_a_clock_step_cannot_wipe_the_history(store):
    now = int(time.time())
    conn_events.record(store, "connect", "ancient", now=now - 40 * 86400)
    conn_events.record(store, "connect", "recent", now=now)
    conn_events.record(store, "connect", "later", now=now + 1)

    # Aged out and gone. The prune floor is anchored to the newest event ALREADY stored, so it
    # lags one write behind — the same trade the traffic tables make, and for the same reason:
    # a lone far-future event (an RTC that came back wrong) must not be able to wipe the history.
    assert [row["detail"] for row in store.list_events(limit=10)] == ["recent", "later"]

    conn_events.record(store, "connect", "future", now=now + 400 * 86400)
    assert len(store.list_events(limit=10)) == 3          # nothing deleted by the clock step


# --- what the log adds up to ---

def test_incidents_are_down_up_spans_and_an_open_one_is_marked():
    now = 10_000
    events = [
        {"ts": 1_000, "kind": "connect", "detail": "connected to a"},
        {"ts": 2_000, "kind": "disconnect", "detail": "node disconnected"},
        {"ts": 2_060, "kind": "xray-stop", "detail": "still down"},      # inside the open span
        {"ts": 2_300, "kind": "connect", "detail": "connected to a"},
        {"ts": 3_000, "kind": "kill-switch", "detail": "enabled"},       # history, not downtime
        {"ts": 9_800, "kind": "all-nodes-down", "detail": "nothing to fail over to"},
    ]
    spans = conn_events.incidents(events, now=now)

    assert [(s["started"], s["seconds"], s["ongoing"]) for s in spans] == [
        (9_800, 200, True),        # newest first, still open, closed at `now`
        (2_000, 300, False),
    ]
    assert conn_events.downtime(spans, since=0) == 500
    # A window that starts mid-incident counts only the part inside it.
    assert conn_events.downtime(spans, since=2_100) == 200 + 200


def test_the_events_endpoint_reports_the_window_the_list_and_the_downtime(settings, stub_xray):
    c = _client(settings, stub_xray)
    _login(c)
    store = c.app.state.app_state.store
    now = int(time.time())
    conn_events.record(store, "disconnect", "node disconnected", now=now - 600)
    conn_events.record(store, "connect", "connected to nl-ams-03", now=now - 300)
    conn_events.record(store, "failover", "de-fra-01 → nl-ams-03", now=now - 100)

    body = c.get("/api/events?window_sec=86400").json()

    assert [e["kind"] for e in body["events"]] == ["disconnect", "connect", "failover"]
    assert body["window_sec"] == 86_400 and body["downtime_sec"] == 300
    assert body["incidents"][0]["seconds"] == 300 and body["incidents"][0]["ongoing"] is False
    # A kind filter narrows the list but never the incidents: they are about the whole window.
    filtered = c.get("/api/events?kind=failover").json()
    assert [e["kind"] for e in filtered["events"]] == ["failover"]
    assert len(filtered["incidents"]) == 1


def test_failovers_in_the_last_day_are_counted_in_the_table_not_over_the_last_40_events(settings, stub_xray):
    c = _client(settings, stub_xray)
    _login(c)
    store = c.app.state.app_state.store
    now = int(time.time())
    for index in range(60):                       # a busy day of ordinary events
        conn_events.record(store, "connect", f"c{index}", now=now - 3_000 + index)
    conn_events.record(store, "failover", "a → b", now=now - 4_000)
    conn_events.record(store, "failover", "b → c", now=now - 100_000)      # older than 24 h

    assert c.get("/api/status").json()["failovers_24h"] == 1


# --- A7: usage ---

def test_days_and_totals_are_added_up_in_sql(store):
    day = 24 * 60
    base = (int(time.time()) // 60 // day) * day                # today's first minute, UTC
    store.add_traffic_minute(base + 10, 1_000, 2_000)
    store.add_traffic_minute(base + 20, 500, 500)
    store.add_traffic_minute(base - day + 5, 7_000, 3_000)      # yesterday

    days = store.traffic_days(since_min=base - 3 * day)
    assert [(row["up_bytes"], row["down_bytes"]) for row in days] == [(7_000, 3_000), (1_500, 2_500)]
    assert store.traffic_total(since_min=base) == {"up_bytes": 1_500, "down_bytes": 2_500}
    assert store.traffic_total(since_min=base - day, until_min=base) == {"up_bytes": 7_000, "down_bytes": 3_000}


def test_the_usage_endpoint_answers_with_the_cap_and_the_clock(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    store = c.app.state.app_state.store
    now_min = int(time.time()) // 60
    store.add_traffic_minute(now_min - 5, 3_000_000, 7_000_000)

    c.put("/api/settings", json={"traffic_cap_gb": 100, "traffic_cap_reset_day": 5}, headers=h)
    body = c.get("/api/traffic/usage").json()

    assert body["today"] >= 10_000_000 and body["week"] >= 10_000_000
    assert body["cap_bytes"] == 100_000_000_000 and body["cap_reset_day"] == 5
    assert body["retention_days"] == 90 and body["server_now"] > 0
    assert any(row["up_bytes"] == 3_000_000 for row in body["days"])


def test_the_cap_is_bounded_so_a_typo_cannot_become_a_figure(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    assert c.put("/api/settings", json={"traffic_cap_gb": -1}, headers=h).status_code == 422
    assert c.put("/api/settings", json={"traffic_cap_reset_day": 31}, headers=h).status_code == 422
    assert c.put("/api/settings", json={"traffic_cap_reset_day": 28}, headers=h).status_code == 200


@pytest.mark.parametrize("day_of_month,reset_day,months_back,expected_month", [
    (17, 5, 0, "this month's 5th"),
    (3, 5, 0, "last month's 5th"),        # before the reset day: the period began last month
    (17, 5, 1, "last month's 5th"),
])
def test_the_month_starts_on_the_reset_day(day_of_month, reset_day, months_back, expected_month):
    """A provider's allowance runs reset-day to reset-day, so "this month" must too."""
    from datetime import datetime, timezone
    from pi_gw_panel.api.routes import _month_start_min
    now = int(datetime(2026, 6, day_of_month, 12, tzinfo=timezone.utc).timestamp())

    start = datetime.fromtimestamp(_month_start_min(now, 0, reset_day, months_back) * 60, tz=timezone.utc)

    assert start.day == reset_day
    assert start.month == (6 if expected_month.startswith("this") else 5)
