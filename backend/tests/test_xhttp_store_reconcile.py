from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.models import Node, Subscription
from pi_gw_panel.subs.reconcile import reconcile


def _store() -> NodeStore:
    conn = connect(":memory:")
    init_schema(conn)
    return NodeStore(conn)


def _xhttp(name, path):
    return Node(id=None, name=name, address="ru.pinusm.ru", port=443, uuid="u1",
                transport="xhttp", network="xhttp", security="tls", sni="ru.pinusm.ru",
                path=path, host="ru.pinusm.ru", mode="stream-up", alpn="h2,http/1.1")


def test_node_roundtrip_preserves_xhttp_fields():
    st = _store()
    n = st.get_node(st.add_node(_xhttp("FI2", "/p-fi2")))
    assert (n.network, n.security) == ("xhttp", "tls")
    assert (n.path, n.host, n.mode, n.alpn) == ("/p-fi2", "ru.pinusm.ru", "stream-up", "h2,http/1.1")


def test_reconcile_keeps_xhttp_variants_sharing_addr_port_uuid():
    st = _store()
    sid = st.add_subscription(Subscription(id=None, name="s", url="u"))
    parsed = [_xhttp("FI2v", "/p-fi2v"), _xhttp("FI2x", "/p-fi2x"), _xhttp("FI3", "/p-fi3")]
    counts = reconcile(st, sid, parsed, active_node_id=None)
    assert counts["added"] == 3                                   # NOT collapsed to 1
    nodes = st.list_nodes_for_sub(sid)
    assert [n.path for n in nodes] == ["/p-fi2v", "/p-fi2x", "/p-fi3"]


def test_reconcile_sets_position_in_subscription_order():
    st = _store()
    sid = st.add_subscription(Subscription(id=None, name="s", url="u"))
    reconcile(st, sid, [_xhttp("a", "/a"), _xhttp("b", "/b"), _xhttp("c", "/c")], None)
    nodes = st.list_nodes_for_sub(sid)
    assert [n.position for n in nodes] == [0, 1, 2]


def test_reconcile_reorder_on_resync_follows_source():
    st = _store()
    sid = st.add_subscription(Subscription(id=None, name="s", url="u"))
    reconcile(st, sid, [_xhttp("a", "/a"), _xhttp("b", "/b")], None)
    # next sync flips the order — display must follow the source, not insertion id
    reconcile(st, sid, [_xhttp("b", "/b"), _xhttp("a", "/a")], None)
    nodes = st.list_nodes_for_sub(sid)
    assert [n.path for n in nodes] == ["/b", "/a"]
