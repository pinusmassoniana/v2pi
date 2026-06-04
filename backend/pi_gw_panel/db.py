import os
import sqlite3


def connect(db_path: str, check_same_thread: bool = True) -> sqlite3.Connection:
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            address TEXT NOT NULL,
            port INTEGER NOT NULL,
            uuid TEXT NOT NULL,
            transport TEXT NOT NULL DEFAULT 'vision',
            sni TEXT NOT NULL DEFAULT '',
            public_key TEXT NOT NULL DEFAULT '',
            short_id TEXT NOT NULL DEFAULT '',
            fingerprint TEXT NOT NULL DEFAULT 'chrome',
            flow TEXT NOT NULL DEFAULT 'xtls-rprx-vision'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.commit()
    migrate(conn)


def _migration_1(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            injection_json TEXT NOT NULL DEFAULT '{}',
            interval_sec INTEGER NOT NULL DEFAULT 0,
            last_fetched TEXT,
            last_status TEXT,
            last_path TEXT
        )
        """
    )
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    if "subscription_id" not in cols:
        conn.execute(
            "ALTER TABLE nodes ADD COLUMN subscription_id INTEGER REFERENCES subscriptions(id)")
    if "stale" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN stale INTEGER NOT NULL DEFAULT 0")


def _migration_2(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tuning_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            fingerprint TEXT NOT NULL DEFAULT 'chrome',
            frag_enabled INTEGER NOT NULL DEFAULT 0,
            frag_packets TEXT NOT NULL DEFAULT 'tlshello',
            frag_length TEXT NOT NULL DEFAULT '100-200',
            frag_interval TEXT NOT NULL DEFAULT '10-20',
            mux_enabled INTEGER NOT NULL DEFAULT 0,
            doh_enabled INTEGER NOT NULL DEFAULT 1,
            doh_url TEXT NOT NULL DEFAULT '',
            quic TEXT NOT NULL DEFAULT 'allow'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS routing_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position INTEGER NOT NULL,
            type TEXT NOT NULL,
            value TEXT NOT NULL,
            action TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS node_health (
            node_id INTEGER PRIMARY KEY,
            last_tcp_ok INTEGER, last_tcp_ms INTEGER,
            last_real_ok INTEGER, last_real_ms INTEGER,
            egress_ip TEXT, checked_at TEXT,
            fail_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    if "tuning_profile_id" not in cols:
        conn.execute(
            "ALTER TABLE nodes ADD COLUMN tuning_profile_id INTEGER REFERENCES tuning_profiles(id)")

    def g(key: str, default: str) -> str:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # Seed a 'default' profile from the Wave-1 global toggle settings (so nothing changes).
    conn.execute(
        """INSERT INTO tuning_profiles
           (name, fingerprint, frag_enabled, frag_packets, frag_length, frag_interval,
            mux_enabled, doh_enabled, doh_url, quic)
           VALUES ('default','chrome',?,?,?,?,?,?,?,'allow')""",
        (1 if g("frag_enabled", "0") == "1" else 0, g("frag_packets", "tlshello"),
         g("frag_length", "100-200"), g("frag_interval", "10-20"),
         1 if g("mux_enabled", "0") == "1" else 0, 1 if g("doh_enabled", "1") == "1" else 0,
         g("doh_url", "")))
    pid = conn.execute("SELECT id FROM tuning_profiles WHERE name='default'").fetchone()["id"]
    conn.execute(
        "INSERT INTO settings(key,value) VALUES('default_profile_id',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(pid),))


def _migration_3(conn: sqlite3.Connection) -> None:
    # XHTTP-over-TLS stream fields + per-node subscription order.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    for name, ddl in (
        ("network", "TEXT NOT NULL DEFAULT 'tcp'"),
        ("security", "TEXT NOT NULL DEFAULT 'reality'"),
        ("path", "TEXT NOT NULL DEFAULT ''"),
        ("host", "TEXT NOT NULL DEFAULT ''"),
        ("mode", "TEXT NOT NULL DEFAULT ''"),
        ("alpn", "TEXT NOT NULL DEFAULT ''"),
        ("position", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE nodes ADD COLUMN {name} {ddl}")


# (version, fn) ascending; each runs once when user_version < version.
_MIGRATIONS = [(1, _migration_1), (2, _migration_2), (3, _migration_3)]


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for v, fn in _MIGRATIONS:
        if version < v:
            fn(conn)
            conn.execute(f"PRAGMA user_version = {int(v)}")
            conn.commit()
