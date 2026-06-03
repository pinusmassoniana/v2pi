from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.models import Node, Subscription


def test_node_roundtrip(settings):
    conn = connect(settings.db_path)
    init_schema(conn)
    store = NodeStore(conn)

    nid = store.add_node(Node(id=None, name="n1", address="1.2.3.4", port=47000, uuid="u-1"))
    assert isinstance(nid, int)

    got = store.get_node(nid)
    assert got.name == "n1"
    assert got.port == 47000
    assert [n.id for n in store.list_nodes()] == [nid]
    assert got.transport == "vision"
    assert got.flow == "xtls-rprx-vision"
    assert got.fingerprint == "chrome"


def test_settings_kv(settings):
    conn = connect(settings.db_path)
    init_schema(conn)
    store = NodeStore(conn)
    store.set_setting("active_node_id", "7")
    assert store.get_setting("active_node_id") == "7"
    assert store.get_setting("missing") is None


def test_node_update_delete_and_identity(settings):
    conn = connect(settings.db_path)
    init_schema(conn)
    s = NodeStore(conn)
    nid = s.add_node(Node(id=None, name="n", address="a", port=1, uuid="u"))
    n = s.get_node(nid)
    assert n.subscription_id is None and n.stale is False
    n.name = "renamed"
    s.update_node(n)
    assert s.get_node(nid).name == "renamed"
    assert s.get_node_by_identity("a", 1, "u").id == nid
    s.mark_stale(nid, True)
    assert s.get_node(nid).stale is True
    s.delete_node(nid)
    assert s.get_node(nid) is None


def test_subscription_crud_and_delete_detaches_nodes(settings):
    conn = connect(settings.db_path)
    init_schema(conn)
    s = NodeStore(conn)
    sid = s.add_subscription(Subscription(id=None, name="sub", url="https://x/y",
                                          injection={"headers": {"x-hwid": "{machine_id}"}},
                                          interval_sec=3600))
    assert s.get_subscription(sid).injection["headers"]["x-hwid"] == "{machine_id}"
    nid = s.add_node(Node(id=None, name="n", address="a", port=1, uuid="u", subscription_id=sid))
    assert [n.id for n in s.list_nodes_for_sub(sid)] == [nid]
    s.delete_subscription(sid)
    assert s.get_subscription(sid) is None
    assert s.get_node(nid).subscription_id is None
