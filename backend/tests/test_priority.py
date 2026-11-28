from datetime import datetime, timezone

from pi_gw_panel.controller import ApplyResult
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.health import failover, priority
from pi_gw_panel.health.priority import decide, due_nodes
from pi_gw_panel.models import Node, NodeHealth
from pi_gw_panel.nodes.store import NodeStore

NOW = 100_000.0


def _ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _up(i):
    return NodeHealth(node_id=i, last_real_ok=True, fail_count=0, checked_at=_ts(NOW - 30))


def _down(i, fails=3):
    return NodeHealth(node_id=i, last_real_ok=False, fail_count=fails, checked_at=_ts(NOW - 30))


def _decide(health, active, since=None):
    return decide([1, 2, 3, 4], health, active, since or {}, NOW,
                  hysteresis=3, failback_sec=1200, ttl=180)


def test_dead_active_skips_dead_standbys_straight_to_next_up():
    health = {1: _down(1), 2: _down(2), 3: _down(3, fails=1), 4: _up(4)}
    assert _decide(health, 1) == 4


def test_dead_active_and_whole_list_down_stays():
    assert _decide({1: _down(1), 2: _down(2), 3: _down(3), 4: _down(4)}, 1) is None


def test_below_hysteresis_no_switch():
    assert _decide({1: _down(1, fails=2), 2: _up(2)}, 1) is None


def test_stale_standby_is_not_available():
    stale = _up(2)
    stale.checked_at = _ts(NOW - 600)
    assert _decide({1: _down(1), 2: stale, 3: _up(3)}, 1) == 3


def test_failback_needs_full_window():
    health = {1: _up(1), 2: _up(2), 3: _up(3)}
    assert _decide(health, 3, since={1: NOW - 1199, 2: NOW - 5000}) == 2   # 1 not yet, 2 is
    assert _decide(health, 3, since={1: NOW - 1200, 2: NOW - 5000}) == 1   # highest wins
    assert _decide(health, 3, since={}) is None


def test_never_moves_down_while_active_healthy():
    assert _decide({1: _up(1), 2: _up(2)}, 1, since={2: 0}) is None


def test_manual_pick_off_list_returns_to_list():
    assert _decide({9: _up(9), 2: _up(2)}, 9, since={2: NOW - 1300}) == 2


def test_disconnected_does_nothing():
    assert _decide({1: _up(1)}, None, since={1: 0}) is None


def test_due_nodes_batches_oldest_first_and_skips_active():
    last = {1: 0, 2: 50, 3: 10, 4: 990, 5: 20}
    assert due_nodes([1, 2, 3, 4, 5], 1, last, 1000, 60, 2) == [3, 5]
    assert due_nodes([1, 2, 3], None, {}, 1000, 60, 5) == [1, 2, 3]


class _State:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.supervisor, self.net = None, None
        self.xray_bin = settings.xray_bin


def _state(settings):
    conn = connect(settings.db_path, check_same_thread=False)
    init_schema(conn)
    return _State(NodeStore(conn), settings)


def _add(st, n):
    return st.store.add_node(Node(id=None, name=f"n{n}", address=f"{n}.{n}.{n}.{n}", port=443,
                                  uuid=f"u{n}"))


def test_note_real_tracks_unbroken_run(settings):
    st = _state(settings)
    priority.note_real(st.store, 7, True, 10)
    priority.note_real(st.store, 7, True, 20)
    assert priority.ok_since(st.store) == {7: 10}
    priority.note_real(st.store, 7, False, 30)
    assert priority.ok_since(st.store) == {}


def test_failover_run_uses_list_and_ignores_off_list_nodes(settings):
    st = _state(settings)
    a, b, c = _add(st, 1), _add(st, 2), _add(st, 3)
    priority.set_order(st.store, [a, b])           # c is healthy but not on the list
    st.store.set_setting("active_node_id", str(a))
    for h in (_down(a), _down(b), _up(c)):
        st.store.upsert_health(h)
    applied = []

    def fake_apply(node, s, sup, net, store=None, xray_bin=None):
        applied.append(node.id)
        store.set_setting("active_node_id", str(node.id))
        return ApplyResult(ok=True)

    assert failover.run(st, now=NOW, apply_fn=fake_apply) is None and applied == []
    st.store.upsert_health(_up(b))
    assert failover.run(st, now=NOW, apply_fn=fake_apply) == b and applied == [b]


def test_probe_list_checks_one_batch_and_records(settings):
    st = _state(settings)
    ids = [_add(st, n) for n in range(1, 6)]
    priority.set_order(st.store, ids)
    st.store.set_setting("active_node_id", str(ids[0]))
    st.store.set_setting("failover_probe_batch", "2")
    seen = []

    def fake_real(node, *_a, **_k):
        seen.append(node.id)
        return (node.id != ids[1], 50, None, None)

    last: dict[int, float] = {}
    priority.probe_list(st, NOW, _ts(NOW), fake_real, last_probed=last)
    assert sorted(seen) == ids[1:3]
    assert priority.ok_since(st.store) == {ids[2]: NOW}
    assert st.store.get_health(ids[1]).fail_count == 1
    priority.probe_list(st, NOW + 20, _ts(NOW + 20), fake_real, last_probed=last)
    assert sorted(seen) == ids[1:5]                # next batch, not the same nodes again


def test_failover_order_api(settings, stub_xray):
    from tests.conftest import _client, _login
    client = _client(settings, stub_xray)
    headers = {"X-CSRF-Token": _login(client)}
    ids = [client.post("/api/nodes", headers=headers,
                       json={"name": f"n{i}", "address": f"1.2.3.{i}", "port": 443,
                             "uuid": "11111111-1111-1111-1111-111111111111"}).json()["id"]
           for i in (1, 2)]
    assert client.get("/api/failover").json() == {"order": [], "ok_since": {}}
    r = client.put("/api/failover/order", headers=headers, json={"order": [ids[1], ids[0]]})
    assert r.status_code == 200 and r.json()["order"] == [ids[1], ids[0]]
    assert client.put("/api/failover/order", headers=headers,
                      json={"order": [ids[0], ids[0]]}).status_code == 422
    assert client.put("/api/failover/order", headers=headers, json={"order": [999]}).status_code == 422
    s = client.get("/api/settings").json()
    assert (s["failback_minutes"], s["failover_real_interval"], s["failover_probe_batch"]) == (20, 60, 3)
    assert client.put("/api/settings", headers=headers, json={"failover_probe_batch": 0}).status_code == 422
