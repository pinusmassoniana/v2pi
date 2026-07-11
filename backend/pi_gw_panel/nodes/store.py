import json
import sqlite3
import threading
import time
from pi_gw_panel.models import Node, Subscription, TuningProfile, RoutingRule, NodeHealth


class _Result:
    """Materialized result of one locked execute, so callers keep the existing
    `execute(...).fetchone()/.fetchall()/.lastrowid` pattern with no DB access after
    the lock is released."""
    __slots__ = ("lastrowid", "_rows")

    def __init__(self, lastrowid, rows):
        self.lastrowid = lastrowid
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _SafeConn:
    """Serialize all access to a single sqlite connection shared across threads
    (uvicorn's pool, the probe/health executors, the traffic recorder). A bare shared
    connection raises `sqlite3.InterfaceError: bad parameter or other API misuse` under
    concurrent use; one re-entrant lock spanning execute+fetch (and commit) prevents it."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.RLock()

    def execute(self, sql: str, params=()) -> _Result:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return _Result(cur.lastrowid, cur.fetchall())

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    # transaction context (`with conn:` — used by the backup restore): hold the lock for
    # the whole transaction so it's both atomic and serialized against other threads.
    def __enter__(self):
        self._lock.acquire()
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            return self._conn.__exit__(exc_type, exc, tb)
        finally:
            self._lock.release()

_NODE_COLS = ("name", "address", "port", "uuid", "transport", "sni",
              "public_key", "short_id", "fingerprint", "flow",
              "network", "security", "path", "host", "mode", "alpn", "note",
              "subscription_id", "stale", "tuning_profile_id", "position")

# Scalar profile columns (attr name == column name). `noises` is stored separately as JSON.
_PROFILE_COLS = ("name", "fingerprint", "frag_enabled", "frag_packets", "frag_length",
                 "frag_interval", "mux_enabled", "doh_enabled", "doh_url", "quic",
                 "noise_enabled", "xhttp_padding", "xmux_max_concurrency", "xmux_max_connections",
                 "mux_concurrency", "xudp_proxy_udp443", "alpn", "tls_min", "tls_max")
_PROFILE_BOOL_COLS = {"frag_enabled", "mux_enabled", "doh_enabled", "noise_enabled"}
_INSERT_NODE = (
    f"INSERT INTO nodes ({', '.join(_NODE_COLS)}) "
    f"VALUES ({', '.join(['?'] * len(_NODE_COLS))})"
)


def _node_values(node: Node) -> tuple:
    return tuple(int(getattr(node, c)) if c == "stale" else getattr(node, c)
                 for c in _NODE_COLS)


def _row_to_node(row: sqlite3.Row) -> Node:
    return Node(
        id=row["id"], name=row["name"], address=row["address"], port=row["port"],
        uuid=row["uuid"], transport=row["transport"], sni=row["sni"],
        public_key=row["public_key"], short_id=row["short_id"],
        fingerprint=row["fingerprint"], flow=row["flow"],
        network=row["network"], security=row["security"], path=row["path"],
        host=row["host"], mode=row["mode"], alpn=row["alpn"], note=row["note"],
        subscription_id=row["subscription_id"], stale=bool(row["stale"]),
        tuning_profile_id=row["tuning_profile_id"], position=row["position"],
    )


def _row_to_sub(row: sqlite3.Row) -> Subscription:
    return Subscription(
        id=row["id"], name=row["name"], url=row["url"],
        injection=json.loads(row["injection_json"] or "{}"),
        interval_sec=row["interval_sec"], enabled=bool(row["enabled"]),
        default_profile_id=row["default_profile_id"],
        last_fetched=row["last_fetched"], last_status=row["last_status"],
        last_path=row["last_path"], last_error=row["last_error"],
        up_bytes=row["up_bytes"], down_bytes=row["down_bytes"],
        total_bytes=row["total_bytes"], expire_at=row["expire_at"],
    )


def _profile_values(p: TuningProfile) -> tuple:
    return tuple(int(getattr(p, c)) if c in _PROFILE_BOOL_COLS else getattr(p, c)
                 for c in _PROFILE_COLS)


def _row_to_profile(row: sqlite3.Row) -> TuningProfile:
    keys = row.keys()

    def g(name, default=""):
        return row[name] if name in keys else default
    return TuningProfile(
        id=row["id"], name=row["name"], fingerprint=row["fingerprint"],
        frag_enabled=bool(row["frag_enabled"]), frag_packets=row["frag_packets"],
        frag_length=row["frag_length"], frag_interval=row["frag_interval"],
        mux_enabled=bool(row["mux_enabled"]), doh_enabled=bool(row["doh_enabled"]),
        doh_url=row["doh_url"], quic=row["quic"],
        noise_enabled=bool(g("noise_enabled", 0)),
        noises=json.loads(g("noises_json", "[]") or "[]"),
        xhttp_padding=g("xhttp_padding"), xmux_max_concurrency=g("xmux_max_concurrency"),
        xmux_max_connections=g("xmux_max_connections"), mux_concurrency=g("mux_concurrency"),
        xudp_proxy_udp443=g("xudp_proxy_udp443"), alpn=g("alpn"),
        tls_min=g("tls_min"), tls_max=g("tls_max"),
    )


def _row_to_health(row: sqlite3.Row) -> NodeHealth:
    def b(v):
        return None if v is None else bool(v)
    keys = row.keys()
    lat = json.loads(row["lat_history"] or "[]") if "lat_history" in keys else []
    return NodeHealth(
        node_id=row["node_id"], last_tcp_ok=b(row["last_tcp_ok"]), last_tcp_ms=row["last_tcp_ms"],
        last_http_ok=b(row["last_http_ok"]), last_http_ms=row["last_http_ms"],
        last_real_ok=b(row["last_real_ok"]), last_real_ms=row["last_real_ms"],
        egress_ip=row["egress_ip"], checked_at=row["checked_at"], fail_count=row["fail_count"],
        egress_ip6=(row["egress_ip6"] if "egress_ip6" in keys else None),
        lat_history=lat,
    )


class NodeStore:
    def __init__(self, conn: sqlite3.Connection):
        # wrap in a lock-serialized proxy so concurrent threads can't corrupt the
        # single shared connection (sqlite "bad parameter or other API misuse")
        self._conn = _SafeConn(conn)

    # --- nodes ---
    def add_node(self, node: Node) -> int:
        cur = self._conn.execute(_INSERT_NODE, _node_values(node))
        self._conn.commit()
        return int(cur.lastrowid)

    def get_node(self, node_id: int) -> Node | None:
        row = self._conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return _row_to_node(row) if row else None

    def get_node_by_identity(self, sub_id: int | None, address: str, port: int, uuid: str,
                             path: str = "") -> Node | None:
        # Scoped to the owning subscription so two subscriptions with the same server each
        # keep their own copy instead of fighting over one row. `IS` matches NULL too, so a
        # sub-refresh scoped to a real sub_id can never steal a manual (NULL) or other-sub node.
        row = self._conn.execute(
            "SELECT * FROM nodes WHERE subscription_id IS ? AND address = ? AND port = ? "
            "AND uuid = ? AND path = ?",
            (sub_id, address, port, uuid, path)).fetchone()
        return _row_to_node(row) if row else None

    def list_nodes(self) -> list[Node]:
        rows = self._conn.execute("SELECT * FROM nodes ORDER BY position, id").fetchall()
        return [_row_to_node(r) for r in rows]

    def list_nodes_for_sub(self, sub_id: int) -> list[Node]:
        rows = self._conn.execute(
            "SELECT * FROM nodes WHERE subscription_id = ? ORDER BY position, id",
            (sub_id,)).fetchall()
        return [_row_to_node(r) for r in rows]

    def node_counts_by_sub(self) -> dict[int, int]:
        """{subscription_id: node count} in one query (avoids an N+1 in the subs list)."""
        rows = self._conn.execute(
            "SELECT subscription_id, COUNT(*) AS n FROM nodes "
            "WHERE subscription_id IS NOT NULL GROUP BY subscription_id").fetchall()
        return {r["subscription_id"]: r["n"] for r in rows}

    def update_node(self, node: Node) -> None:
        assert node.id is not None
        sets = ", ".join(f"{c} = ?" for c in _NODE_COLS)
        self._conn.execute(f"UPDATE nodes SET {sets} WHERE id = ?",
                           (*_node_values(node), node.id))
        self._conn.commit()

    def delete_node(self, node_id: int) -> None:
        # Drop the node's health row too (no FK/ON DELETE on node_health) so deletes —
        # including the churn from every sub refresh — don't leave orphans behind.
        # One transaction so both deletes are atomic (audit P1).
        with self._conn:
            self._conn.execute("DELETE FROM node_health WHERE node_id = ?", (node_id,))
            self._conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))

    def reorder_nodes(self, node_ids: list[int]) -> None:
        """Set ``position`` = list index for the given node ids (manual-node reorder).
        Ids not in the list are left untouched."""
        # One transaction so all position updates land (or roll back) together (audit P1).
        with self._conn:
            for i, nid in enumerate(node_ids):
                self._conn.execute("UPDATE nodes SET position = ? WHERE id = ?", (i, nid))

    def detach_nodes(self, node_ids: list[int]) -> None:
        """Detach nodes from their subscription (→ manual Servers); the rows survive so a
        live/active connection is kept."""
        for nid in node_ids:
            self._conn.execute("UPDATE nodes SET subscription_id = NULL WHERE id = ?", (nid,))
        self._conn.commit()

    def mark_stale(self, node_id: int, stale: bool) -> None:
        self._conn.execute("UPDATE nodes SET stale = ? WHERE id = ?", (int(stale), node_id))
        self._conn.commit()

    # --- settings (k/v) ---
    def set_setting(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        self._conn.commit()

    def get_setting(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def get_settings_map(self) -> dict[str, str]:
        """All settings in one query — for hot read paths that need several at once."""
        rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

    # --- api tokens ---
    def create_token(self, name: str, scope: str, token_hash: str, prefix: str) -> dict:
        """Insert a token (caller generates the secret + hash). Returns the metadata row only —
        the caller adds the one-time full secret to its response; the secret is never stored."""
        now = int(time.time())
        cur = self._conn.execute(
            "INSERT INTO api_tokens(name, token_hash, scope, prefix, created_at) "
            "VALUES(?, ?, ?, ?, ?)", (name, token_hash, scope, prefix, now))
        self._conn.commit()
        return {"id": int(cur.lastrowid), "name": name, "scope": scope, "prefix": prefix,
                "created_at": now, "last_used_at": None}

    def list_tokens(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, name, scope, prefix, created_at, last_used_at FROM api_tokens "
            "ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def get_token_by_hash(self, token_hash: str) -> dict | None:
        row = self._conn.execute(
            "SELECT id, scope, prefix FROM api_tokens WHERE token_hash = ?", (token_hash,)).fetchone()
        return dict(row) if row else None

    def touch_token(self, token_id: int) -> None:
        """Throttled last-used stamp: at most ~once/60s, so token auth doesn't write per request."""
        now = int(time.time())
        self._conn.execute(
            "UPDATE api_tokens SET last_used_at = ? WHERE id = ? "
            "AND (last_used_at IS NULL OR last_used_at < ?)", (now, token_id, now - 60))
        self._conn.commit()

    def delete_token(self, token_id: int) -> bool:
        found = self._conn.execute(
            "SELECT 1 FROM api_tokens WHERE id = ?", (token_id,)).fetchone()
        self._conn.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
        self._conn.commit()
        return found is not None

    # --- audit log (N2) ---
    _AUDIT_CAP = 2000

    def add_audit(self, ts: int, actor: str, method: str, path: str, status: int) -> None:
        """Append one mutation record, pruning everything older than the newest _AUDIT_CAP
        rows in the same commit (O(1) — id is monotonic)."""
        # One transaction so the insert + prune are atomic (audit P1).
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO audit_log(ts, actor, method, path, status) VALUES(?, ?, ?, ?, ?)",
                (ts, actor, method, path, status))
            self._conn.execute("DELETE FROM audit_log WHERE id <= ?",
                               (int(cur.lastrowid) - self._AUDIT_CAP,))

    def list_audit(self, limit: int = 100) -> list[dict]:
        """Newest-first audit entries."""
        rows = self._conn.execute(
            "SELECT ts, actor, method, path, status FROM audit_log ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, self._AUDIT_CAP)),)).fetchall()
        return [dict(r) for r in rows]

    # --- traffic minutes (N4) ---
    _TRAFFIC_RETENTION_MIN = 90 * 24 * 60   # ~90 days of 1-min samples

    def add_traffic_minute(self, ts_min: int, up_bytes: int, down_bytes: int) -> None:
        """Upsert one minute of proxy bytes (additive on conflict — a recorder restart within
        the same minute must not lose the earlier slice). Prunes beyond the retention window."""
        # One transaction so the upsert + prune are atomic (audit P1).
        with self._conn:
            self._conn.execute(
                "INSERT INTO traffic_minutes(ts_min, up_bytes, down_bytes) VALUES(?, ?, ?) "
                "ON CONFLICT(ts_min) DO UPDATE SET up_bytes = up_bytes + excluded.up_bytes, "
                "down_bytes = down_bytes + excluded.down_bytes",
                (ts_min, up_bytes, down_bytes))
            self._conn.execute("DELETE FROM traffic_minutes WHERE ts_min < ?",
                               (ts_min - self._TRAFFIC_RETENTION_MIN,))

    def traffic_minutes(self, since_min: int) -> list[dict]:
        """Per-minute samples (ascending) since `since_min` (unix//60)."""
        rows = self._conn.execute(
            "SELECT ts_min, up_bytes, down_bytes FROM traffic_minutes "
            "WHERE ts_min >= ? ORDER BY ts_min", (since_min,)).fetchall()
        return [dict(r) for r in rows]

    # --- subscriptions ---
    def add_subscription(self, sub: Subscription) -> int:
        cur = self._conn.execute(
            "INSERT INTO subscriptions(name, url, injection_json, interval_sec, enabled, "
            "default_profile_id) VALUES(?, ?, ?, ?, ?, ?)",
            (sub.name, sub.url, json.dumps(sub.injection), sub.interval_sec,
             int(sub.enabled), sub.default_profile_id))
        self._conn.commit()
        return int(cur.lastrowid)

    def get_subscription(self, sub_id: int) -> Subscription | None:
        row = self._conn.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()
        return _row_to_sub(row) if row else None

    def list_subscriptions(self) -> list[Subscription]:
        rows = self._conn.execute("SELECT * FROM subscriptions ORDER BY id").fetchall()
        return [_row_to_sub(r) for r in rows]

    def update_subscription(self, sub: Subscription) -> None:
        assert sub.id is not None
        self._conn.execute(
            "UPDATE subscriptions SET name=?, url=?, injection_json=?, interval_sec=?, "
            "enabled=?, default_profile_id=?, last_fetched=?, last_status=?, last_path=?, "
            "last_error=?, up_bytes=?, down_bytes=?, total_bytes=?, expire_at=? WHERE id=?",
            (sub.name, sub.url, json.dumps(sub.injection), sub.interval_sec,
             int(sub.enabled), sub.default_profile_id, sub.last_fetched, sub.last_status,
             sub.last_path, sub.last_error, sub.up_bytes, sub.down_bytes, sub.total_bytes,
             sub.expire_at, sub.id))
        self._conn.commit()

    def delete_subscription(self, sub_id: int) -> None:
        # Detach nodes (→ manual) so a live/active connection survives, then drop the sub.
        # One transaction so the detach + delete are atomic (audit P1).
        with self._conn:
            self._conn.execute(
                "UPDATE nodes SET subscription_id = NULL WHERE subscription_id = ?", (sub_id,))
            self._conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))

    # --- tuning profiles ---
    def add_profile(self, p: TuningProfile) -> int:
        cols = _PROFILE_COLS + ("noises_json",)
        cur = self._conn.execute(
            f"INSERT INTO tuning_profiles ({', '.join(cols)}) "
            f"VALUES ({', '.join(['?'] * len(cols))})",
            (*_profile_values(p), json.dumps(p.noises)))
        self._conn.commit()
        return int(cur.lastrowid)

    def get_profile(self, profile_id: int) -> TuningProfile | None:
        row = self._conn.execute(
            "SELECT * FROM tuning_profiles WHERE id = ?", (profile_id,)).fetchone()
        return _row_to_profile(row) if row else None

    def list_profiles(self) -> list[TuningProfile]:
        rows = self._conn.execute("SELECT * FROM tuning_profiles ORDER BY id").fetchall()
        return [_row_to_profile(r) for r in rows]

    def update_profile(self, p: TuningProfile) -> None:
        assert p.id is not None
        cols = _PROFILE_COLS + ("noises_json",)
        sets = ", ".join(f"{c} = ?" for c in cols)
        self._conn.execute(f"UPDATE tuning_profiles SET {sets} WHERE id = ?",
                           (*_profile_values(p), json.dumps(p.noises), p.id))
        self._conn.commit()

    def delete_profile(self, profile_id: int) -> None:
        # Detach referencing nodes (→ inherit the global default) before removing the profile.
        # One transaction so the detach + delete are atomic (audit P1).
        with self._conn:
            self._conn.execute(
                "UPDATE nodes SET tuning_profile_id = NULL WHERE tuning_profile_id = ?", (profile_id,))
            self._conn.execute("DELETE FROM tuning_profiles WHERE id = ?", (profile_id,))

    def get_default_profile(self) -> TuningProfile | None:
        did = self.get_setting("default_profile_id")
        return self.get_profile(int(did)) if did else None

    def set_default_profile(self, profile_id: int) -> None:
        self.set_setting("default_profile_id", str(profile_id))

    # --- routing rules (ordered) ---
    def get_routing(self) -> list[RoutingRule]:
        rows = self._conn.execute("SELECT * FROM routing_rules ORDER BY position").fetchall()
        out = []
        for r in rows:
            keys = r.keys()
            out.append(RoutingRule(
                id=r["id"], position=r["position"], type=r["type"], value=r["value"],
                action=r["action"],
                enabled=bool(r["enabled"]) if "enabled" in keys else True,
                label=r["label"] if "label" in keys else ""))
        return out

    def replace_routing(self, rules: list[RoutingRule]) -> None:
        # Whole-list replace: positions are re-derived from list order (0..n-1).
        # One transaction so the DELETE + N INSERTs are atomic — a concurrent commit can't
        # flush the empty-table state between them (audit P1).
        with self._conn:
            self._conn.execute("DELETE FROM routing_rules")
            for i, r in enumerate(rules):
                self._conn.execute(
                    "INSERT INTO routing_rules(position, type, value, action, enabled, label) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    (i, r.type, r.value, r.action, int(getattr(r, "enabled", True)),
                     getattr(r, "label", "")))

    # --- node health ---
    def upsert_health(self, h: NodeHealth) -> None:
        self._conn.execute(
            """INSERT INTO node_health
               (node_id, last_tcp_ok, last_tcp_ms, last_http_ok, last_http_ms,
                last_real_ok, last_real_ms, egress_ip, egress_ip6, checked_at, fail_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(node_id) DO UPDATE SET
                 last_tcp_ok=excluded.last_tcp_ok, last_tcp_ms=excluded.last_tcp_ms,
                 last_http_ok=excluded.last_http_ok, last_http_ms=excluded.last_http_ms,
                 last_real_ok=excluded.last_real_ok, last_real_ms=excluded.last_real_ms,
                 egress_ip=excluded.egress_ip, egress_ip6=excluded.egress_ip6,
                 checked_at=excluded.checked_at, fail_count=excluded.fail_count""",
            (h.node_id,
             None if h.last_tcp_ok is None else int(h.last_tcp_ok), h.last_tcp_ms,
             None if h.last_http_ok is None else int(h.last_http_ok), h.last_http_ms,
             None if h.last_real_ok is None else int(h.last_real_ok), h.last_real_ms,
             h.egress_ip, h.egress_ip6, h.checked_at, h.fail_count))
        self._conn.commit()

    def get_health(self, node_id: int) -> NodeHealth | None:
        row = self._conn.execute(
            "SELECT * FROM node_health WHERE node_id = ?", (node_id,)).fetchone()
        return _row_to_health(row) if row else None

    def list_health(self) -> list[NodeHealth]:
        rows = self._conn.execute("SELECT * FROM node_health ORDER BY node_id").fetchall()
        return [_row_to_health(r) for r in rows]

    def record_latency(self, node_id: int, ms: int, cap: int = 20) -> None:
        """Append one latency sample to the node's history ring (NN4). Kept as a separate
        column update so it survives the health upsert; capped at `cap` most-recent samples."""
        # Read-modify-write in one transaction so concurrent samples don't lose updates (audit P1).
        with self._conn:
            row = self._conn.execute(
                "SELECT lat_history FROM node_health WHERE node_id = ?", (node_id,)).fetchone()
            if row is None:
                return
            hist = json.loads(row["lat_history"] or "[]")
            hist.append(int(ms))
            self._conn.execute("UPDATE node_health SET lat_history = ? WHERE node_id = ?",
                               (json.dumps(hist[-cap:]), node_id))
