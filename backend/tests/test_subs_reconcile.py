import threading
import time

from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.models import Node, Subscription
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.subs.reconcile import reconcile
from pi_gw_panel.subs.service import refresh


def _store(settings):
    conn = connect(settings.db_path, check_same_thread=False)
    init_schema(conn)
    return NodeStore(conn)


def test_reconcile_add_update_remove(settings):
    s = _store(settings)
    sid = s.add_subscription(Subscription(id=None, name="x", url="u"))
    a = s.add_node(Node(id=None, name="A", address="1.1.1.1", port=443, uuid="ua", subscription_id=sid))
    s.add_node(Node(id=None, name="B", address="2.2.2.2", port=443, uuid="ub", subscription_id=sid))
    parsed = [Node(id=None, name="A2", address="1.1.1.1", port=443, uuid="ua"),
              Node(id=None, name="C", address="3.3.3.3", port=443, uuid="uc")]
    counts = reconcile(s, sid, parsed, active_node_id=None)
    assert counts == {"added": 1, "updated": 1, "removed": 1,
                      "active_changed": False, "active_replacement": None, "skipped_deletes": 0}
    names = {n.address: n.name for n in s.list_nodes_for_sub(sid)}
    assert names == {"1.1.1.1": "A2", "3.3.3.3": "C"}  # B removed
    assert s.get_node(a).id == a  # A kept its id across the update


def test_reconcile_active_config_change_flags_restart(settings):
    # Same identity, but the active server's reality key rotated → caller must restart on it.
    s = _store(settings)
    sid = s.add_subscription(Subscription(id=None, name="x", url="u"))
    act = s.add_node(Node(id=None, name="A", address="1.1.1.1", port=443, uuid="ua",
                          public_key="OLD", subscription_id=sid))
    counts = reconcile(s, sid, [Node(id=None, name="A", address="1.1.1.1", port=443,
                                     uuid="ua", public_key="NEW")], active_node_id=act)
    assert counts["active_changed"] is True and counts["active_replacement"] is None
    assert s.get_node(act).public_key == "NEW"


def test_reconcile_active_cosmetic_change_no_restart(settings):
    # Only the display name changed — not a config field → no restart (no needless tunnel blip).
    s = _store(settings)
    sid = s.add_subscription(Subscription(id=None, name="x", url="u"))
    act = s.add_node(Node(id=None, name="A", address="1.1.1.1", port=443, uuid="ua",
                          public_key="K", subscription_id=sid))
    counts = reconcile(s, sid, [Node(id=None, name="renamed", address="1.1.1.1", port=443,
                                     uuid="ua", public_key="K")], active_node_id=act)
    assert counts["active_changed"] is False and counts["active_replacement"] is None


def test_reconcile_single_server_identity_rotation_replaces_active(settings):
    # The one server rotated address+uuid (new identity) → move the connection to it.
    s = _store(settings)
    sid = s.add_subscription(Subscription(id=None, name="x", url="u"))
    act = s.add_node(Node(id=None, name="A", address="1.1.1.1", port=443, uuid="ua", subscription_id=sid))
    counts = reconcile(s, sid, [Node(id=None, name="A2", address="2.2.2.2", port=443, uuid="ub")],
                       active_node_id=act)
    rep = counts["active_replacement"]
    assert rep is not None
    assert s.get_node(rep).address == "2.2.2.2" and not s.get_node(rep).stale
    assert s.get_node(act).stale is True


def test_reconcile_two_subs_same_server_get_independent_copies(settings):
    # Two subscriptions with an identical server must each own their OWN node — sub2's
    # reconcile must not steal/reassign sub1's row (identity is scoped per subscription).
    s = _store(settings)
    s1 = s.add_subscription(Subscription(id=None, name="a", url="u1"))
    s2 = s.add_subscription(Subscription(id=None, name="b", url="u2"))
    def server():
        return Node(id=None, name="N", address="1.1.1.1", port=443, uuid="u")
    reconcile(s, s1, [server()], active_node_id=None)
    reconcile(s, s2, [server()], active_node_id=None)
    a, b = s.list_nodes_for_sub(s1), s.list_nodes_for_sub(s2)
    assert len(a) == 1 and len(b) == 1            # sub2 did not collapse into sub1
    assert a[0].id != b[0].id                     # distinct rows, one per subscription


def test_reconcile_protects_active_node(settings):
    s = _store(settings)
    sid = s.add_subscription(Subscription(id=None, name="x", url="u"))
    act = s.add_node(Node(id=None, name="A", address="1.1.1.1", port=443, uuid="ua", subscription_id=sid))
    counts = reconcile(s, sid, [], active_node_id=act)  # empty feed (likely transient) → don't trust it
    # floor-guard: an empty/near-empty response no longer deletes OR marks-stale — it leaves the
    # store untouched so one bad fetch can't disrupt the live connection.
    assert counts["removed"] == 0 and counts["skipped_deletes"] == 1
    n = s.get_node(act)
    assert n is not None and n.stale is False   # active left intact on an untrusted empty feed


def test_reconcile_keeps_reality_locations_sharing_one_endpoint(settings):
    # A reality feed presents many concurrent exit configs on one IP:port+uuid, differing only
    # by SNI/shortId (each a distinct city). Identity includes sni/short_id, so they must NOT
    # collapse — the store keeps every advertised location, not just the shared endpoint.
    s = _store(settings)
    sid = s.add_subscription(Subscription(id=None, name="x", url="u"))
    parsed = [Node(id=None, name=f"city{i}", address="1.1.1.1", port=443, uuid="ua",
                   security="reality", public_key="PBK", sni=f"cdn{i}.example.com",
                   short_id=f"{i:016x}") for i in range(5)]
    counts = reconcile(s, sid, parsed, active_node_id=None)
    assert counts["added"] == 5
    assert len(s.list_nodes_for_sub(sid)) == 5     # not collapsed to 1


def test_refresh_end_to_end(monkeypatch, settings):
    s = _store(settings)
    sid = s.add_subscription(Subscription(id=None, name="s", url="https://h/s"))
    sub = s.get_subscription(sid)
    monkeypatch.setattr(
        "pi_gw_panel.subs.service.fetch",
        lambda url, inj, tok, *, proxy: (
            '[{"name":"a","address":"1.1.1.1","port":443,"uuid":"u"}]', "direct", {}))

    class FakeSup:
        def status(self):
            return {"running": False, "pid": None}

    class FakeState:
        store = s
        supervisor = FakeSup()

    fs = FakeState()
    fs.settings = settings
    res = refresh(fs, sub)
    assert res["added"] == 1 and res["path"] == "direct"
    assert s.list_nodes_for_sub(sid)[0].address == "1.1.1.1"
    assert s.get_subscription(sid).last_path == "direct"


def _fake_state(s, settings):
    supervisor = type("Sup", (), {"status": lambda self: {"running": False}})()
    return type("S", (), {"store": s, "settings": settings, "net": object(),
                          "supervisor": supervisor, "xray_bin": None})()


def test_restart_active_reapplies_on_config_change(monkeypatch, settings):
    from pi_gw_panel.subs import service
    s = _store(settings)
    sid = s.add_subscription(Subscription(id=None, name="x", url="u"))
    act = s.add_node(Node(id=None, name="A", address="1.1.1.1", port=443, uuid="u", subscription_id=sid))
    calls = []
    monkeypatch.setattr(service, "apply_node", lambda node, *a, **k: calls.append(node.id))
    service._restart_active(_fake_state(s, settings), act,
                            {"active_changed": True, "active_replacement": None})
    assert calls == [act]
    calls.clear()
    service._restart_active(_fake_state(s, settings), act,
                            {"active_changed": False, "active_replacement": None})
    assert calls == []                       # nothing relevant changed → no tunnel blip


def test_restart_active_switches_and_drops_stale_on_replacement(monkeypatch, settings):
    from pi_gw_panel.subs import service
    from pi_gw_panel.controller import ApplyResult
    s = _store(settings)
    sid = s.add_subscription(Subscription(id=None, name="x", url="u"))
    old = s.add_node(Node(id=None, name="old", address="1.1.1.1", port=443, uuid="u",
                          subscription_id=sid, stale=True))
    new = s.add_node(Node(id=None, name="new", address="2.2.2.2", port=443, uuid="v", subscription_id=sid))
    calls = []
    monkeypatch.setattr(service, "apply_node",
                        lambda node, *a, **k: calls.append(node.id) or ApplyResult(ok=True))
    service._restart_active(_fake_state(s, settings), old,
                            {"active_changed": False, "active_replacement": new})
    assert calls == [new]                    # reconnected on the rotated server
    assert s.get_node(old) is None           # stale old node cleaned up after the switch


def test_anomalous_shrink_is_stale_then_delete_on_confirmation(settings):
    s = _store(settings)
    sid = s.add_subscription(Subscription(id=None, name="x", url="u"))
    for i in range(10):
        s.add_node(Node(id=None, name=f"n{i}", address=f"1.1.1.{i + 1}", port=443,
                        uuid=f"u{i}", subscription_id=sid))
    parsed = [Node(id=None, name=f"n{i}", address=f"1.1.1.{i + 1}", port=443,
                   uuid=f"u{i}") for i in range(4)]

    first = reconcile(s, sid, parsed, active_node_id=None)
    assert first["removed"] == 0 and first["skipped_deletes"] == 6
    assert sum(node.stale for node in s.list_nodes_for_sub(sid)) == 6

    second = reconcile(s, sid, parsed, active_node_id=None)
    assert second["removed"] == 6 and second["skipped_deletes"] == 0
    assert len(s.list_nodes_for_sub(sid)) == 4


def test_returning_node_clears_first_shrink_stale_marker(settings):
    s = _store(settings)
    sid = s.add_subscription(Subscription(id=None, name="x", url="u"))
    for i in range(4):
        s.add_node(Node(id=None, name=f"n{i}", address=f"1.1.1.{i + 1}", port=443,
                        uuid=f"u{i}", subscription_id=sid))
    one = [Node(id=None, name="n0", address="1.1.1.1", port=443, uuid="u0")]
    reconcile(s, sid, one, active_node_id=None)
    assert sum(node.stale for node in s.list_nodes_for_sub(sid)) == 3

    all_nodes = [Node(id=None, name=f"n{i}", address=f"1.1.1.{i + 1}", port=443,
                      uuid=f"u{i}") for i in range(4)]
    reconcile(s, sid, all_nodes, active_node_id=None)
    assert not any(node.stale for node in s.list_nodes_for_sub(sid))


def test_different_anomalous_shrink_does_not_confirm_deletes(settings):
    s = _store(settings)
    sid = s.add_subscription(Subscription(id=None, name="x", url="u"))
    for i in range(6):
        s.add_node(Node(id=None, name=f"n{i}", address=f"1.1.1.{i + 1}", port=443,
                        uuid=f"u{i}", subscription_id=sid))
    first = [Node(id=None, name="n0", address="1.1.1.1", port=443, uuid="u0")]
    different = [Node(id=None, name="n1", address="1.1.1.2", port=443, uuid="u1")]
    reconcile(s, sid, first, active_node_id=None)
    result = reconcile(s, sid, different, active_node_id=None)
    assert result["removed"] == 0 and len(s.list_nodes_for_sub(sid)) == 6
    assert reconcile(s, sid, different, active_node_id=None)["removed"] == 5


def test_reconcile_rolls_back_all_rows_on_store_failure(monkeypatch, settings):
    s = _store(settings)
    sid = s.add_subscription(Subscription(id=None, name="x", url="u"))
    for i in range(4):
        s.add_node(Node(id=None, name=f"old{i}", address=f"1.1.1.{i + 1}", port=443,
                        uuid=f"u{i}", subscription_id=sid))
    parsed = [Node(id=None, name="changed0", address="1.1.1.1", port=443, uuid="u0"),
              Node(id=None, name="changed1", address="1.1.1.2", port=443, uuid="u1")]
    original_delete = s.delete_node

    def fail_after_delete(node_id):
        original_delete(node_id)
        raise RuntimeError("disk fault")

    monkeypatch.setattr(s, "delete_node", fail_after_delete)
    import pytest
    with pytest.raises(RuntimeError, match="disk fault"):
        reconcile(s, sid, parsed, active_node_id=None)
    assert [node.name for node in s.list_nodes_for_sub(sid)] == [f"old{i}" for i in range(4)]


def test_failed_active_reapply_restores_old_row_and_retries(monkeypatch, settings):
    from pi_gw_panel.controller import ApplyResult
    from pi_gw_panel.subs import service

    s = _store(settings)
    sid = s.add_subscription(Subscription(id=None, name="x", url="https://h/sub"))
    active = s.add_node(Node(id=None, name="n", address="1.1.1.1", port=443, uuid="u",
                             public_key="OLD", subscription_id=sid))
    s.set_setting("active_node_id", str(active))
    monkeypatch.setattr(service, "fetch", lambda *a, **k: (
        '[{"name":"n","address":"1.1.1.1","port":443,"uuid":"u","public_key":"NEW"}]',
        "direct", {}))
    calls = []
    monkeypatch.setattr(service, "apply_node", lambda *a, **k:
                        calls.append(1) or ApplyResult(ok=False, error="reload failed"))
    state = _fake_state(s, settings)

    first = refresh(state, s.get_subscription(sid))
    second = refresh(state, s.get_subscription(sid))
    assert first["ok"] is False and second["ok"] is False
    assert len(calls) == 2
    assert s.get_node(active).public_key == "OLD"


def test_zero_valid_nodes_is_error_and_does_not_advance_last_fetched(monkeypatch, settings):
    from pi_gw_panel.subs import service

    s = _store(settings)
    sid = s.add_subscription(Subscription(id=None, name="x", url="https://h/sub"))
    monkeypatch.setattr(service, "fetch", lambda *a, **k: ("<html>captcha</html>", "direct", {}))
    result = refresh(_fake_state(s, settings), s.get_subscription(sid))
    saved = s.get_subscription(sid)
    assert result["ok"] is False and "zero valid nodes" in result["error"]
    assert saved.last_fetched is None and saved.last_status.startswith("error:")


def test_refresh_lifecycle_update_does_not_overwrite_concurrent_patch(monkeypatch, settings):
    from pi_gw_panel.subs import service

    s = _store(settings)
    sid = s.add_subscription(Subscription(id=None, name="old", url="https://h/sub"))
    entered = threading.Event()
    release = threading.Event()

    def blocked_fetch(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return ('[{"name":"n","address":"1.1.1.1","port":443,"uuid":"u"}]',
                "direct", {})

    monkeypatch.setattr(service, "fetch", blocked_fetch)
    state = _fake_state(s, settings)
    thread = threading.Thread(target=refresh, args=(state, s.get_subscription(sid)))
    thread.start()
    assert entered.wait(2)
    edited = s.get_subscription(sid)
    edited.name = "new"
    edited.enabled = False
    s.update_subscription(edited)
    release.set()
    thread.join(2)

    saved = s.get_subscription(sid)
    assert saved.name == "new" and saved.enabled is False
    assert saved.last_status.startswith("ok:")


def test_same_subscription_refreshes_are_serialized(monkeypatch, settings):
    from pi_gw_panel.subs import service

    s = _store(settings)
    sid = s.add_subscription(Subscription(id=None, name="x", url="https://h/sub"))
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def blocked_fetch(*args, **kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
            if calls == 1:
                entered.set()
        if calls == 1:
            assert release.wait(2)
        return ('[{"name":"n","address":"1.1.1.1","port":443,"uuid":"u"}]',
                "direct", {})

    monkeypatch.setattr(service, "fetch", blocked_fetch)
    state = _fake_state(s, settings)
    threads = [threading.Thread(target=refresh, args=(state, s.get_subscription(sid)))
               for _ in range(2)]
    threads[0].start()
    assert entered.wait(2)
    threads[1].start()
    time.sleep(0.05)
    assert calls == 1
    release.set()
    for thread in threads:
        thread.join(2)
    assert calls == 2 and len(s.list_nodes_for_sub(sid)) == 1


def test_url_changed_during_fetch_discards_old_feed(monkeypatch, settings):
    from pi_gw_panel.subs import service

    s = _store(settings)
    sid = s.add_subscription(Subscription(id=None, name="x", url="https://old/sub"))
    entered = threading.Event()
    release = threading.Event()
    result = {}

    def blocked_fetch(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return ('[{"name":"old","address":"1.1.1.1","port":443,"uuid":"u"}]',
                "direct", {})

    monkeypatch.setattr(service, "fetch", blocked_fetch)
    state = _fake_state(s, settings)
    thread = threading.Thread(
        target=lambda: result.update(refresh(state, s.get_subscription(sid))))
    thread.start()
    assert entered.wait(2)
    edited = s.get_subscription(sid)
    edited.url = "https://new/sub"
    s.update_subscription(edited)
    release.set()
    thread.join(2)

    assert result["ok"] is False and "changed during fetch" in result["error"]
    assert s.list_nodes_for_sub(sid) == []
