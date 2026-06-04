import os
import sqlite3


def connect(db_path: str, check_same_thread: bool = True) -> sqlite3.Connection:
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Tuned for the Pi's SD card: WAL + synchronous=NORMAL skips the per-commit fsync (readers
    # also stop blocking the writer), busy_timeout avoids spurious "database is locked", and a
    # memory temp store keeps sort/scratch off the card. WAL persists; the rest are per-connection.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA temp_store = MEMORY")
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


def _migration_4(conn: sqlite3.Connection) -> None:
    # Separate HTTPS-handshake health from the through-tunnel "real" health.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(node_health)").fetchall()}
    for name in ("last_http_ok", "last_http_ms"):
        if name not in cols:
            conn.execute(f"ALTER TABLE node_health ADD COLUMN {name} INTEGER")


def _migration_5(conn: sqlite3.Connection) -> None:
    # Subscription lifecycle + provider metadata: enable/disable, a per-sub default tuning
    # profile new nodes inherit, full last-error text, and Subscription-Userinfo quota/expiry.
    # (subscriptions is created in migration 1; guard so it's a no-op if 1 hasn't run.)
    if not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='subscriptions'"
    ).fetchone():
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(subscriptions)").fetchall()}
    for name, ddl in (
        ("enabled", "INTEGER NOT NULL DEFAULT 1"),
        ("default_profile_id", "INTEGER REFERENCES tuning_profiles(id)"),
        ("last_error", "TEXT"),
        ("up_bytes", "INTEGER"),
        ("down_bytes", "INTEGER"),
        ("total_bytes", "INTEGER"),
        ("expire_at", "INTEGER"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE subscriptions ADD COLUMN {name} {ddl}")


def _migration_6(conn: sqlite3.Connection) -> None:
    # Per-node latency history (JSON array of recent HTTPS-handshake ms) for the sparkline.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(node_health)").fetchall()}
    if "lat_history" not in cols:
        conn.execute("ALTER TABLE node_health ADD COLUMN lat_history TEXT NOT NULL DEFAULT '[]'")


def _migration_7(conn: sqlite3.Connection) -> None:
    # Per-rule enable/disable + label (routing_rules is created in migration 2).
    if not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='routing_rules'"
    ).fetchone():
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(routing_rules)").fetchall()}
    if "enabled" not in cols:
        conn.execute("ALTER TABLE routing_rules ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
    if "label" not in cols:
        conn.execute("ALTER TABLE routing_rules ADD COLUMN label TEXT NOT NULL DEFAULT ''")


def _migration_9(conn: sqlite3.Connection) -> None:
    # Per-node free-text operator note / label (searchable in the Nodes panel).
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    if "note" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN note TEXT NOT NULL DEFAULT ''")


def _migration_10(conn: sqlite3.Connection) -> None:
    # Per-node IPv6 egress (shown alongside the v4 egress on the dashboard + Nodes tab).
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(node_health)").fetchall()}
    if "egress_ip6" not in cols:
        conn.execute("ALTER TABLE node_health ADD COLUMN egress_ip6 TEXT")


def _migration_8(conn: sqlite3.Connection) -> None:
    # Anti-DPI tuning knobs: freedom noises, xhttp padding/xmux, mux concurrency/xudp, tls alpn/version.
    if not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tuning_profiles'"
    ).fetchone():
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tuning_profiles)").fetchall()}
    for name, ddl in (
        ("noise_enabled", "INTEGER NOT NULL DEFAULT 0"),
        ("noises_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("xhttp_padding", "TEXT NOT NULL DEFAULT ''"),
        ("xmux_max_concurrency", "TEXT NOT NULL DEFAULT ''"),
        ("xmux_max_connections", "TEXT NOT NULL DEFAULT ''"),
        ("mux_concurrency", "TEXT NOT NULL DEFAULT ''"),
        ("xudp_proxy_udp443", "TEXT NOT NULL DEFAULT ''"),
        ("alpn", "TEXT NOT NULL DEFAULT ''"),
        ("tls_min", "TEXT NOT NULL DEFAULT ''"),
        ("tls_max", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE tuning_profiles ADD COLUMN {name} {ddl}")


# (version, fn) ascending; each runs once when user_version < version.
_MIGRATIONS = [(1, _migration_1), (2, _migration_2), (3, _migration_3), (4, _migration_4),
               (5, _migration_5), (6, _migration_6), (7, _migration_7), (8, _migration_8),
               (9, _migration_9), (10, _migration_10)]


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for v, fn in _MIGRATIONS:
        if version < v:
            fn(conn)
            conn.execute(f"PRAGMA user_version = {int(v)}")
            conn.commit()
