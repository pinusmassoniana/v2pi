"""Coverage for the optimization pass (OB1 WAL, OB2 tail, OB4 counts, OB6 settings map)."""
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.models import Node, Subscription
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel import logs as logs_mod


def test_db_uses_wal_and_normal(tmp_path):
    conn = connect(str(tmp_path / "t.sqlite"))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1   # NORMAL


def test_tail_reads_from_end_across_blocks(tmp_path):
    p = tmp_path / "big.log"
    p.write_text("".join(f"line {i}\n" for i in range(5000)))   # ~> one 8 KB block
    assert logs_mod.tail(str(p), 3) == ["line 4997", "line 4998", "line 4999"]
    assert logs_mod.tail(str(p), 10)[0] == "line 4990"
    assert logs_mod.tail(str(p), 999999)[0] == "line 0"          # asking for more than exists
    assert logs_mod.tail(str(tmp_path / "missing.log"), 5) == []


def test_node_counts_by_sub(tmp_path):
    conn = connect(str(tmp_path / "t.sqlite")); init_schema(conn)
    s = NodeStore(conn)
    a = s.add_subscription(Subscription(id=None, name="a", url="u"))
    b = s.add_subscription(Subscription(id=None, name="b", url="u"))
    s.add_node(Node(id=None, name="n1", address="1.1.1.1", port=443, uuid="u1", subscription_id=a))
    s.add_node(Node(id=None, name="n2", address="2.2.2.2", port=443, uuid="u2", subscription_id=a))
    s.add_node(Node(id=None, name="m", address="3.3.3.3", port=443, uuid="u3"))   # manual
    assert s.node_counts_by_sub() == {a: 2}                     # b has none, manual excluded


def test_get_settings_map(tmp_path):
    conn = connect(str(tmp_path / "t.sqlite")); init_schema(conn)
    s = NodeStore(conn)
    s.set_setting("k1", "v1"); s.set_setting("k2", "v2")
    m = s.get_settings_map()
    assert m["k1"] == "v1" and m["k2"] == "v2"
