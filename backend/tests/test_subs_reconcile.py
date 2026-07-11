from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.models import Node, Subscription
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.subs.reconcile import reconcile
from pi_gw_panel.subs.service import refresh


def _store(settings):
    conn = connect(settings.db_path)
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
    return type("S", (), {"store": s, "settings": settings, "net": object(),
                          "supervisor": object(), "xray_bin": None})()


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
