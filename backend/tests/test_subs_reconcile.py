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
    assert counts == {"added": 1, "updated": 1, "removed": 1}
    names = {n.address: n.name for n in s.list_nodes_for_sub(sid)}
    assert names == {"1.1.1.1": "A2", "3.3.3.3": "C"}  # B removed
    assert s.get_node(a).id == a  # A kept its id across the update


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
    counts = reconcile(s, sid, [], active_node_id=act)  # active vanishes from feed
    assert counts["removed"] == 0
    n = s.get_node(act)
    assert n is not None and n.stale is True


def test_refresh_end_to_end(monkeypatch, settings):
    s = _store(settings)
    sid = s.add_subscription(Subscription(id=None, name="s", url="https://h/s"))
    sub = s.get_subscription(sid)
    monkeypatch.setattr(
        "pi_gw_panel.subs.service.fetch",
        lambda url, inj, tok, *, proxy: (
            '[{"name":"a","address":"1.1.1.1","port":443,"uuid":"u"}]', "direct"))

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
