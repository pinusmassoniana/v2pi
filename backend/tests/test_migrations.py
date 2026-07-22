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
