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
