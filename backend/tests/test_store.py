import pytest

from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.models import Node, Subscription, NodeHealth, TuningProfile


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
    assert s.get_node_by_identity(None, "a", 1, "u").id == nid   # manual node → sub_id None
    s.mark_stale(nid, True)
    assert s.get_node(nid).stale is True
    s.delete_node(nid)
    assert s.get_node(nid) is None


def test_store_concurrent_access_is_serialized(settings):
    # Many threads hammering one shared connection must not raise sqlite
    # "bad parameter or other API misuse" — the race that 500'd /status under load.
    import threading
    conn = connect(settings.db_path, check_same_thread=False)
    init_schema(conn)
    s = NodeStore(conn)
    nid = s.add_node(Node(id=None, name="n", address="a", port=1, uuid="u"))
    errors: list = []

    def hammer():
        try:
            for i in range(150):
                s.set_setting("k", str(i))
                s.get_setting("k")
                s.list_nodes()
                s.upsert_health(NodeHealth(node_id=nid, last_tcp_ok=True, last_tcp_ms=i))
        except Exception as e:  # noqa: BLE001 — capture for the assert
            errors.append(e)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [], errors[0]


def test_store_transaction_isolation_and_rollback(settings):
    import threading

    conn = connect(settings.db_path, check_same_thread=False)
    init_schema(conn)
    store = NodeStore(conn)
    first_write = threading.Event()
    release_first = threading.Event()
    second_done = threading.Event()
    errors: list[Exception] = []

    def first_writer() -> None:
        try:
            with store.transaction():
                store.set_setting("tx-first", "must-roll-back")
                first_write.set()
                assert release_first.wait(2)
                raise RuntimeError("rollback")
        except RuntimeError:
            pass
        except Exception as exc:  # pragma: no cover - diagnostic path
            errors.append(exc)

    def second_writer() -> None:
        try:
            assert first_write.wait(2)
            store.set_setting("tx-second", "committed")
            second_done.set()
        except Exception as exc:  # pragma: no cover - diagnostic path
            errors.append(exc)

    first = threading.Thread(target=first_writer)
    second = threading.Thread(target=second_writer)
    first.start()
    assert first_write.wait(2)
    second.start()
    assert not second_done.wait(0.1), "second writer escaped the first transaction lock"
    release_first.set()
    first.join(2)
    second.join(2)

    assert errors == []
    assert store.get_setting("tx-first") is None
    assert store.get_setting("tx-second") == "committed"


def test_store_close_closes_connection(settings):
    import sqlite3

    conn = connect(settings.db_path)
    init_schema(conn)
    store = NodeStore(conn)
    store.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        store.get_setting("anything")


def test_corrupt_json_rows_are_contained(settings, caplog):
    import logging

    conn = connect(settings.db_path)
    init_schema(conn)
    store = NodeStore(conn)
    sub_id = store.add_subscription(Subscription(
        id=None, name="sub", url="https://example.test", injection={"ok": True}))
    profile_id = store.add_profile(TuningProfile(id=None, name="profile", noises=[]))
    node_id = store.add_node(Node(id=None, name="n", address="a", port=1, uuid="u"))
    store.upsert_health(NodeHealth(node_id=node_id))
    conn.execute("UPDATE subscriptions SET injection_json = '{bad' WHERE id = ?", (sub_id,))
    conn.execute("UPDATE tuning_profiles SET noises_json = '{bad' WHERE id = ?", (profile_id,))
    conn.execute("UPDATE node_health SET lat_history = '{bad' WHERE node_id = ?", (node_id,))

    with caplog.at_level(logging.WARNING):
        assert store.get_subscription(sub_id).injection == {}
        assert store.get_profile(profile_id).noises == []
        assert store.get_health(node_id).lat_history == []

    assert "invalid JSON" in caplog.text


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
