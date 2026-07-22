import logging
import os
import sqlite3


logger = logging.getLogger(__name__)


def _secure_path(path: str, mode: int, *, kind: str) -> None:
    try:
        os.chmod(path, mode)
    except OSError as exc:
        logger.warning("could not secure %s %s to %03o: %s", kind, path, mode, exc)


def connect(db_path: str, check_same_thread: bool = True) -> sqlite3.Connection:
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
        _secure_path(parent, 0o700, kind="database directory")
    # Create and secure the credential-bearing file before SQLite opens it. Relying on a
    # post-connect chmod leaves a window where the process umask controls its permissions.
    if db_path != ":memory:" and not db_path.startswith("file:"):
        fd = os.open(db_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        _secure_path(db_path, 0o600, kind="database")
    conn = sqlite3.connect(
        db_path, check_same_thread=check_same_thread, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Tuned for the Pi's SD card: WAL + synchronous=NORMAL skips the per-commit fsync (readers
    # also stop blocking the writer), busy_timeout avoids spurious "database is locked", and a
    # memory temp store keeps sort/scratch off the card. WAL persists; the rest are per-connection.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA temp_store = MEMORY")
    # WAL/SHM sidecars may be created by the PRAGMAs above; secure any that exist.
    for p in (db_path, db_path + "-wal", db_path + "-shm"):
        if os.path.exists(p):
            _secure_path(p, 0o600, kind="database")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    _assert_supported_schema(conn)
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
    # Guarded (WHERE NOT EXISTS) so re-applying a partially-run migration can't duplicate it.
    conn.execute(
        """INSERT INTO tuning_profiles
           (name, fingerprint, frag_enabled, frag_packets, frag_length, frag_interval,
            mux_enabled, doh_enabled, doh_url, quic)
           SELECT 'default','chrome',?,?,?,?,?,?,?,'allow'
           WHERE NOT EXISTS (SELECT 1 FROM tuning_profiles WHERE name='default')""",
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


def _migration_11(conn: sqlite3.Connection) -> None:
    # API tokens (read / read-write) for programmatic REST access. The secret is a 256-bit random
    # value shown once at creation; only its SHA-256 hash is stored. `prefix` is a non-secret slice
    # for identifying a row in the token list.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS api_tokens (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT    NOT NULL,
            token_hash   TEXT    NOT NULL UNIQUE,
            scope        TEXT    NOT NULL,
            prefix       TEXT    NOT NULL DEFAULT '',
            created_at   INTEGER NOT NULL,
            last_used_at INTEGER
        )
        """
    )


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


def _migration_12(conn: sqlite3.Connection) -> None:
    # Mutation audit log (N2): who (session user / token prefix) did what (method+path) when.
    # Bounded by the store (oldest rows pruned on insert), so no retention policy needed here.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            ts     INTEGER NOT NULL,
            actor  TEXT    NOT NULL,
            method TEXT    NOT NULL,
            path   TEXT    NOT NULL,
            status INTEGER NOT NULL
        )
        """
    )


def _migration_13(conn: sqlite3.Connection) -> None:
    # Durable traffic history (N4): proxy bytes per minute (ts_min = unix//60), feeding the
    # Dashboard's 24h/7d windows and monthly data-used reports. Pruned to ~90 days on insert.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS traffic_minutes (
            ts_min     INTEGER PRIMARY KEY,
            up_bytes   INTEGER NOT NULL DEFAULT 0,
            down_bytes INTEGER NOT NULL DEFAULT 0
        )
        """
    )


def _migration_14(conn: sqlite3.Connection) -> None:
    """Enforce the identities and ownership relationships used by runtime code."""
    active_row = conn.execute(
        "SELECT value FROM settings WHERE key='active_node_id'").fetchone()
    try:
        active_id = int(active_row["value"]) if active_row else None
    except (TypeError, ValueError):
        active_id = None

    duplicate_groups = conn.execute(
        """SELECT GROUP_CONCAT(id) AS ids
           FROM nodes
           GROUP BY COALESCE(subscription_id, -1), address, port, uuid, path, sni, short_id
           HAVING COUNT(*) > 1""").fetchall()
    for group in duplicate_groups:
        ids = [int(value) for value in group["ids"].split(",")]
        keep = active_id if active_id in ids else min(ids)
        remove = [node_id for node_id in ids if node_id != keep]
        marks = ",".join("?" for _ in remove)
        conn.execute(f"DELETE FROM node_health WHERE node_id IN ({marks})", remove)
        conn.execute(f"DELETE FROM nodes WHERE id IN ({marks})", remove)
        logger.warning("deduplicated node identities: kept id=%s, removed ids=%s", keep, remove)

    # Normalize legacy duplicate/gapped positions before adding the invariant.
    for position, row in enumerate(conn.execute(
            "SELECT id FROM routing_rules ORDER BY position, id").fetchall()):
        conn.execute("UPDATE routing_rules SET position=? WHERE id=?", (position, row["id"]))

    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_nodes_identity
           ON nodes(COALESCE(subscription_id, -1), address, port, uuid, path, sni, short_id)""")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_routing_rules_position ON routing_rules(position)")

    # SQLite cannot add an FK to an existing table, so rebuild it transactionally.
    conn.execute(
        """CREATE TABLE node_health_new (
            node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
            last_tcp_ok INTEGER, last_tcp_ms INTEGER,
            last_real_ok INTEGER, last_real_ms INTEGER,
            egress_ip TEXT, checked_at TEXT,
            fail_count INTEGER NOT NULL DEFAULT 0,
            last_http_ok INTEGER, last_http_ms INTEGER,
            lat_history TEXT NOT NULL DEFAULT '[]',
            egress_ip6 TEXT
        )""")
    conn.execute(
        """INSERT INTO node_health_new
           (node_id,last_tcp_ok,last_tcp_ms,last_real_ok,last_real_ms,egress_ip,checked_at,
            fail_count,last_http_ok,last_http_ms,lat_history,egress_ip6)
           SELECT h.node_id,h.last_tcp_ok,h.last_tcp_ms,h.last_real_ok,h.last_real_ms,
                  h.egress_ip,h.checked_at,h.fail_count,h.last_http_ok,h.last_http_ms,
                  h.lat_history,h.egress_ip6
           FROM node_health h JOIN nodes n ON n.id=h.node_id""")
    conn.execute("DROP TABLE node_health")
    conn.execute("ALTER TABLE node_health_new RENAME TO node_health")

    # Scope CHECK and expiry also require a table rebuild on SQLite.
    conn.execute(
        """CREATE TABLE api_tokens_new (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT    NOT NULL,
            token_hash   TEXT    NOT NULL UNIQUE,
            scope        TEXT    NOT NULL CHECK(scope IN ('monitor','read','readwrite')),
            prefix       TEXT    NOT NULL DEFAULT '',
            created_at   INTEGER NOT NULL,
            last_used_at INTEGER,
            expires_at   INTEGER
        )""")
    invalid = conn.execute(
        "SELECT COUNT(*) AS n FROM api_tokens WHERE scope NOT IN ('monitor','read','readwrite')"
    ).fetchone()["n"]
    if invalid:
        logger.warning("discarding %s API token(s) with invalid legacy scopes", invalid)
    conn.execute(
        """INSERT INTO api_tokens_new
           (id,name,token_hash,scope,prefix,created_at,last_used_at,expires_at)
           SELECT id,name,token_hash,scope,prefix,created_at,last_used_at,NULL
           FROM api_tokens WHERE scope IN ('monitor','read','readwrite')""")
    conn.execute("DROP TABLE api_tokens")
    conn.execute("ALTER TABLE api_tokens_new RENAME TO api_tokens")


def _migration_15(conn: sqlite3.Connection) -> None:
    """Make fail-closed explicit for new installs without overriding an operator choice.

    A fresh database reaches this migration before setup and therefore has no password hash.
    An already-claimed legacy database that never stored the flag keeps its historical fail-open
    behavior; any explicit 0/1 value is always preserved.
    """
    if conn.execute(
            "SELECT 1 FROM settings WHERE key='kill_switch_enabled'").fetchone() is not None:
        return
    claimed = conn.execute(
        "SELECT 1 FROM settings WHERE key='auth_password_hash'").fetchone() is not None
    conn.execute(
        "INSERT INTO settings(key,value) VALUES('kill_switch_enabled',?)",
        ("0" if claimed else "1",))


# (version, fn) ascending; each runs once when user_version < version.
_MIGRATIONS = [(1, _migration_1), (2, _migration_2), (3, _migration_3), (4, _migration_4),
               (5, _migration_5), (6, _migration_6), (7, _migration_7), (8, _migration_8),
               (9, _migration_9), (10, _migration_10), (11, _migration_11),
               (12, _migration_12), (13, _migration_13), (14, _migration_14),
               (15, _migration_15)]


def _assert_supported_schema(conn: sqlite3.Connection) -> int:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    latest = _MIGRATIONS[-1][0]
    if version > latest:
        raise RuntimeError(
            f"newer database schema {version} is not supported by this binary (max {latest}); "
            "back up the database and upgrade pi-gw-panel")
    return version


def migrate(conn: sqlite3.Connection) -> None:
    version = _assert_supported_schema(conn)
    for v, fn in _MIGRATIONS:
        if version < v:
            # Run each migration + its user_version bump in ONE explicit transaction (SQLite
            # DDL is transactional) so a crash mid-migration rolls back cleanly and user_version
            # only advances after fn() fully succeeds. Without the explicit BEGIN the sqlite3
            # module auto-opens a tx only before DML, letting a leading CREATE/ALTER commit on
            # its own and leave a half-applied schema with user_version unchanged.
            conn.execute("BEGIN")
            try:
                fn(conn)
                conn.execute(f"PRAGMA user_version = {int(v)}")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
