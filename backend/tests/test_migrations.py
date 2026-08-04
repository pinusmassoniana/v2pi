import os
import sqlite3

import pytest

from pi_gw_panel.db import connect, init_schema, migrate


def test_fresh_db_has_subscriptions_and_node_columns(tmp_path):
    conn = connect(str(tmp_path / "t.sqlite"))
    init_schema(conn)
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "subscriptions" in tables
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    assert "subscription_id" in cols
    assert "stale" in cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 1
    assert conn.execute(
        "SELECT value FROM settings WHERE key='kill_switch_enabled'").fetchone()["value"] == "1"


def test_kill_switch_migration_preserves_explicit_legacy_choice(tmp_path):
    conn = connect(str(tmp_path / "legacy-kill.sqlite"))
    init_schema(conn)
    conn.execute("UPDATE settings SET value='0' WHERE key='kill_switch_enabled'")
    conn.execute("PRAGMA user_version = 14")
    migrate(conn)
    assert conn.execute(
        "SELECT value FROM settings WHERE key='kill_switch_enabled'").fetchone()["value"] == "0"


def test_upgrade_from_v0_preserves_rows(tmp_path):
    conn = connect(str(tmp_path / "t.sqlite"))
    # simulate a Wave-0 DB: base tables only, user_version 0
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
        "address TEXT NOT NULL, port INTEGER NOT NULL, uuid TEXT NOT NULL, "
        "transport TEXT NOT NULL DEFAULT 'vision', sni TEXT NOT NULL DEFAULT '', "
        "public_key TEXT NOT NULL DEFAULT '', short_id TEXT NOT NULL DEFAULT '', "
        "fingerprint TEXT NOT NULL DEFAULT 'chrome', flow TEXT NOT NULL DEFAULT 'xtls-rprx-vision')")
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO nodes (name,address,port,uuid) VALUES ('n','a',1,'u')")
    conn.commit()
    migrate(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    assert "subscription_id" in cols and "stale" in cols
    assert conn.execute("SELECT stale FROM nodes WHERE name='n'").fetchone()["stale"] == 0
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 1


def test_migrate_is_idempotent(tmp_path):
    conn = connect(str(tmp_path / "t.sqlite"))
    init_schema(conn)
    migrate(conn)  # second run is a no-op
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 1


def test_future_schema_is_refused_before_schema_mutation(tmp_path):
    path = tmp_path / "future.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE future_only (value TEXT)")
    conn.execute("PRAGMA user_version = 999")
    conn.commit()
    conn.close()

    opened = connect(str(path))
    with pytest.raises(RuntimeError, match="newer database schema"):
        init_schema(opened)
    tables = {row[0] for row in opened.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert tables == {"future_only"}
    assert opened.execute("PRAGMA user_version").fetchone()[0] == 999


def test_connect_secures_directory_and_file_before_sqlite_open(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    path = data_dir / "panel.sqlite"
    real_connect = sqlite3.connect
    observed: dict[str, int] = {}

    def observing_connect(db_path, *args, **kwargs):
        observed["dir"] = os.stat(data_dir).st_mode & 0o777
        observed["file"] = os.stat(path).st_mode & 0o777
        return real_connect(db_path, *args, **kwargs)

    monkeypatch.setattr("pi_gw_panel.db.sqlite3.connect", observing_connect)
    conn = connect(str(path))
    conn.close()
    assert observed == {"dir": 0o700, "file": 0o600}


def test_schema_constraints_and_cascade(tmp_path):
    conn = connect(str(tmp_path / "constraints.sqlite"))
    init_schema(conn)
    conn.execute(
        "INSERT INTO nodes(name,address,port,uuid,path,sni,short_id) "
        "VALUES('one','a',1,'u','','','')")
    node_id = conn.execute("SELECT id FROM nodes").fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO nodes(name,address,port,uuid,path,sni,short_id) "
            "VALUES('duplicate','a',1,'u','','','')")
    conn.execute("INSERT INTO node_health(node_id) VALUES(?)", (node_id,))
    conn.execute("DELETE FROM nodes WHERE id=?", (node_id,))
    assert conn.execute("SELECT 1 FROM node_health WHERE node_id=?", (node_id,)).fetchone() is None

    conn.execute(
        "INSERT INTO routing_rules(position,type,value,action) VALUES(0,'domain','x','proxy')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO routing_rules(position,type,value,action) VALUES(0,'domain','y','direct')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO api_tokens(name,token_hash,scope,prefix,created_at) "
            "VALUES('bad','hash','owner','p',0)")


def test_identity_migration_deduplicates_and_preserves_active(tmp_path):
    path = tmp_path / "legacy.sqlite"
    conn = connect(str(path))
    init_schema(conn)
    # Simulate the immediately previous schema, which did not enforce identity uniqueness.
    conn.execute("DROP INDEX IF EXISTS uq_nodes_identity")
    conn.execute("PRAGMA user_version = 13")
    conn.execute(
        "INSERT INTO nodes(name,address,port,uuid,path,sni,short_id) "
        "VALUES('first','a',1,'u','','','')")
    first = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO nodes(name,address,port,uuid,path,sni,short_id) "
        "VALUES('active','a',1,'u','','','')")
    active = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO settings(key,value) VALUES('active_node_id',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(active),))
    migrate(conn)

    rows = conn.execute("SELECT id FROM nodes WHERE address='a'").fetchall()
    assert [row[0] for row in rows] == [active]
    assert active != first


def _legacy_v13(tmp_path, name):
    """A real pre-identity-constraint database: full schema, user_version rewound to 13."""
    conn = connect(str(tmp_path / name))
    init_schema(conn)
    conn.execute("DROP INDEX IF EXISTS uq_nodes_identity")
    conn.execute("PRAGMA user_version = 13")
    return conn


def _add_node(conn, name, **extra):
    cols = "name,address,port,uuid,path,sni,short_id"
    values = [name, "a", 1, "u", "", "", ""]
    for col, value in extra.items():
        cols += f",{col}"
        values.append(value)
    marks = ",".join("?" for _ in values)
    conn.execute(f"INSERT INTO nodes({cols}) VALUES({marks})", values)
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_identity_migration_keeps_the_operator_annotated_duplicate(tmp_path):
    """The keeper is chosen by operator data, not by lowest id: a note typed on the NEWER
    duplicate must survive the dedup."""
    conn = _legacy_v13(tmp_path, "annotated.sqlite")
    profile = conn.execute("SELECT id FROM tuning_profiles WHERE name='default'").fetchone()[0]
    bare = _add_node(conn, "bare")
    annotated = _add_node(conn, "annotated", note="do not lose me", tuning_profile_id=profile)
    migrate(conn)

    rows = conn.execute("SELECT id,note,tuning_profile_id FROM nodes WHERE address='a'").fetchall()
    assert [row["id"] for row in rows] == [annotated]
    assert rows[0]["note"] == "do not lose me"
    assert rows[0]["tuning_profile_id"] == profile
    assert bare != annotated
    assert [row["id"] for row in conn.execute(
        "SELECT id FROM nodes_premigration_v14").fetchall()] == [bare]


def test_identity_migration_archives_every_removed_row_and_merges_operator_data(tmp_path):
    """Nothing is deleted without being archived first, and the pinned active row inherits the
    operator data of the duplicates it supersedes."""
    conn = _legacy_v13(tmp_path, "archive.sqlite")
    profile = conn.execute("SELECT id FROM tuning_profiles WHERE name='default'").fetchone()[0]
    active = _add_node(conn, "active")
    dup_a = _add_node(conn, "dup-a", tuning_profile_id=profile)
    dup_b = _add_node(conn, "dup-b", note="typed later")
    conn.execute("INSERT INTO node_health(node_id,fail_count) VALUES(?,7)", (dup_b,))
    conn.execute("INSERT INTO settings(key,value) VALUES('active_node_id',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(active),))
    migrate(conn)

    survivors = conn.execute("SELECT id,note,tuning_profile_id FROM nodes").fetchall()
    assert [row["id"] for row in survivors] == [active]
    assert survivors[0]["note"] == "typed later"          # merged from dup_b
    assert survivors[0]["tuning_profile_id"] == profile   # merged from dup_a

    archived = {row["id"]: row for row in conn.execute(
        "SELECT * FROM nodes_premigration_v14").fetchall()}
    assert set(archived) == {dup_a, dup_b}                # every deleted row is recoverable
    assert archived[dup_b]["note"] == "typed later"
    assert archived[dup_a]["tuning_profile_id"] == profile
    assert all(row["superseded_by"] == active for row in archived.values())
    assert conn.execute(
        "SELECT fail_count FROM node_health_premigration_v14 WHERE node_id=?",
        (dup_b,)).fetchone()["fail_count"] == 7


def test_identity_migration_leaves_no_dangling_prev_active_pointer(tmp_path):
    conn = _legacy_v13(tmp_path, "prev.sqlite")
    first = _add_node(conn, "first")
    second = _add_node(conn, "second")
    conn.execute("INSERT INTO settings(key,value) VALUES('prev_active_node_id',?)", (str(second),))
    migrate(conn)

    live = {row["id"] for row in conn.execute("SELECT id FROM nodes").fetchall()}
    assert live == {first}
    prev = conn.execute(
        "SELECT value FROM settings WHERE key='prev_active_node_id'").fetchone()["value"]
    assert prev != str(second)               # never left dangling at the deleted id
    assert prev == "" or int(prev) in live   # cleared or repointed to a live row
