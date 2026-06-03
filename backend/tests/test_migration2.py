from pi_gw_panel.db import connect, init_schema


def test_migration2_tables_and_seeded_default_profile(tmp_path):
    conn = connect(str(tmp_path / "t.sqlite"))
    init_schema(conn)
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"tuning_profiles", "routing_rules", "node_health"} <= tables
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    assert "tuning_profile_id" in cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    prof = conn.execute("SELECT * FROM tuning_profiles WHERE name='default'").fetchone()
    assert prof is not None
    did = conn.execute("SELECT value FROM settings WHERE key='default_profile_id'").fetchone()["value"]
    assert int(did) == prof["id"]


def test_migration2_seeds_default_profile_from_wave1_toggles(tmp_path):
    # Simulate an upgrade from a v1 DB that had Wave-1 toggle settings set.
    conn = connect(str(tmp_path / "u.sqlite"))
    conn.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
                 "address TEXT NOT NULL, port INTEGER NOT NULL, uuid TEXT NOT NULL, "
                 "transport TEXT NOT NULL DEFAULT 'vision', sni TEXT NOT NULL DEFAULT '', "
                 "public_key TEXT NOT NULL DEFAULT '', short_id TEXT NOT NULL DEFAULT '', "
                 "fingerprint TEXT NOT NULL DEFAULT 'chrome', flow TEXT NOT NULL DEFAULT 'xtls-rprx-vision')")
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("PRAGMA user_version = 1")  # pretend migration 1 already ran
    conn.execute("INSERT INTO settings(key,value) VALUES('frag_enabled','1'),('mux_enabled','1')")
    conn.commit()
    init_schema(conn)  # applies migration 2
    prof = conn.execute("SELECT * FROM tuning_profiles WHERE name='default'").fetchone()
    assert prof["frag_enabled"] == 1 and prof["mux_enabled"] == 1
