import json
import sqlite3
from pi_gw_panel.models import Node, Subscription, TuningProfile, RoutingRule, NodeHealth

_NODE_COLS = ("name", "address", "port", "uuid", "transport", "sni",
              "public_key", "short_id", "fingerprint", "flow",
              "network", "security", "path", "host", "mode", "alpn",
              "subscription_id", "stale", "tuning_profile_id", "position")

_PROFILE_COLS = ("name", "fingerprint", "frag_enabled", "frag_packets", "frag_length",
                 "frag_interval", "mux_enabled", "doh_enabled", "doh_url", "quic")
_PROFILE_BOOL_COLS = {"frag_enabled", "mux_enabled", "doh_enabled"}
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
        host=row["host"], mode=row["mode"], alpn=row["alpn"],
        subscription_id=row["subscription_id"], stale=bool(row["stale"]),
        tuning_profile_id=row["tuning_profile_id"], position=row["position"],
    )


def _row_to_sub(row: sqlite3.Row) -> Subscription:
    return Subscription(
        id=row["id"], name=row["name"], url=row["url"],
        injection=json.loads(row["injection_json"] or "{}"),
        interval_sec=row["interval_sec"], last_fetched=row["last_fetched"],
        last_status=row["last_status"], last_path=row["last_path"],
    )


def _profile_values(p: TuningProfile) -> tuple:
    return tuple(int(getattr(p, c)) if c in _PROFILE_BOOL_COLS else getattr(p, c)
                 for c in _PROFILE_COLS)


def _row_to_profile(row: sqlite3.Row) -> TuningProfile:
    return TuningProfile(
        id=row["id"], name=row["name"], fingerprint=row["fingerprint"],
        frag_enabled=bool(row["frag_enabled"]), frag_packets=row["frag_packets"],
        frag_length=row["frag_length"], frag_interval=row["frag_interval"],
        mux_enabled=bool(row["mux_enabled"]), doh_enabled=bool(row["doh_enabled"]),
        doh_url=row["doh_url"], quic=row["quic"],
    )


def _row_to_health(row: sqlite3.Row) -> NodeHealth:
    def b(v):
        return None if v is None else bool(v)
    return NodeHealth(
        node_id=row["node_id"], last_tcp_ok=b(row["last_tcp_ok"]), last_tcp_ms=row["last_tcp_ms"],
        last_real_ok=b(row["last_real_ok"]), last_real_ms=row["last_real_ms"],
        egress_ip=row["egress_ip"], checked_at=row["checked_at"], fail_count=row["fail_count"],
    )


class NodeStore:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    # --- nodes ---
    def add_node(self, node: Node) -> int:
        cur = self._conn.execute(_INSERT_NODE, _node_values(node))
        self._conn.commit()
        return int(cur.lastrowid)

    def get_node(self, node_id: int) -> Node | None:
        row = self._conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return _row_to_node(row) if row else None

    def get_node_by_identity(self, address: str, port: int, uuid: str,
                             path: str = "") -> Node | None:
        row = self._conn.execute(
            "SELECT * FROM nodes WHERE address = ? AND port = ? AND uuid = ? AND path = ?",
            (address, port, uuid, path)).fetchone()
        return _row_to_node(row) if row else None

    def list_nodes(self) -> list[Node]:
        rows = self._conn.execute("SELECT * FROM nodes ORDER BY position, id").fetchall()
        return [_row_to_node(r) for r in rows]

    def list_nodes_for_sub(self, sub_id: int) -> list[Node]:
        rows = self._conn.execute(
            "SELECT * FROM nodes WHERE subscription_id = ? ORDER BY position, id",
            (sub_id,)).fetchall()
        return [_row_to_node(r) for r in rows]

    def update_node(self, node: Node) -> None:
        assert node.id is not None
        sets = ", ".join(f"{c} = ?" for c in _NODE_COLS)
        self._conn.execute(f"UPDATE nodes SET {sets} WHERE id = ?",
                           (*_node_values(node), node.id))
        self._conn.commit()

    def delete_node(self, node_id: int) -> None:
        self._conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
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

    # --- subscriptions ---
    def add_subscription(self, sub: Subscription) -> int:
        cur = self._conn.execute(
            "INSERT INTO subscriptions(name, url, injection_json, interval_sec) VALUES(?, ?, ?, ?)",
            (sub.name, sub.url, json.dumps(sub.injection), sub.interval_sec))
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
            "last_fetched=?, last_status=?, last_path=? WHERE id=?",
            (sub.name, sub.url, json.dumps(sub.injection), sub.interval_sec,
             sub.last_fetched, sub.last_status, sub.last_path, sub.id))
        self._conn.commit()

    def delete_subscription(self, sub_id: int) -> None:
        # Detach nodes (→ manual) so a live/active connection survives, then drop the sub.
        self._conn.execute(
            "UPDATE nodes SET subscription_id = NULL WHERE subscription_id = ?", (sub_id,))
        self._conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
        self._conn.commit()

    # --- tuning profiles ---
    def add_profile(self, p: TuningProfile) -> int:
        cur = self._conn.execute(
            f"INSERT INTO tuning_profiles ({', '.join(_PROFILE_COLS)}) "
            f"VALUES ({', '.join(['?'] * len(_PROFILE_COLS))})", _profile_values(p))
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
        sets = ", ".join(f"{c} = ?" for c in _PROFILE_COLS)
        self._conn.execute(f"UPDATE tuning_profiles SET {sets} WHERE id = ?",
                           (*_profile_values(p), p.id))
        self._conn.commit()

    def delete_profile(self, profile_id: int) -> None:
        # Detach referencing nodes (→ inherit the global default) before removing the profile.
        self._conn.execute(
            "UPDATE nodes SET tuning_profile_id = NULL WHERE tuning_profile_id = ?", (profile_id,))
        self._conn.execute("DELETE FROM tuning_profiles WHERE id = ?", (profile_id,))
        self._conn.commit()

    def get_default_profile(self) -> TuningProfile | None:
        did = self.get_setting("default_profile_id")
        return self.get_profile(int(did)) if did else None

    def set_default_profile(self, profile_id: int) -> None:
        self.set_setting("default_profile_id", str(profile_id))

    # --- routing rules (ordered) ---
    def get_routing(self) -> list[RoutingRule]:
        rows = self._conn.execute("SELECT * FROM routing_rules ORDER BY position").fetchall()
        return [RoutingRule(id=r["id"], position=r["position"], type=r["type"],
                            value=r["value"], action=r["action"]) for r in rows]

    def replace_routing(self, rules: list[RoutingRule]) -> None:
        # Whole-list replace: positions are re-derived from list order (0..n-1).
        self._conn.execute("DELETE FROM routing_rules")
        for i, r in enumerate(rules):
            self._conn.execute(
                "INSERT INTO routing_rules(position, type, value, action) VALUES(?, ?, ?, ?)",
                (i, r.type, r.value, r.action))
        self._conn.commit()

    # --- node health ---
    def upsert_health(self, h: NodeHealth) -> None:
        self._conn.execute(
            """INSERT INTO node_health
               (node_id, last_tcp_ok, last_tcp_ms, last_real_ok, last_real_ms,
                egress_ip, checked_at, fail_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(node_id) DO UPDATE SET
                 last_tcp_ok=excluded.last_tcp_ok, last_tcp_ms=excluded.last_tcp_ms,
                 last_real_ok=excluded.last_real_ok, last_real_ms=excluded.last_real_ms,
                 egress_ip=excluded.egress_ip, checked_at=excluded.checked_at,
                 fail_count=excluded.fail_count""",
            (h.node_id,
             None if h.last_tcp_ok is None else int(h.last_tcp_ok), h.last_tcp_ms,
             None if h.last_real_ok is None else int(h.last_real_ok), h.last_real_ms,
             h.egress_ip, h.checked_at, h.fail_count))
        self._conn.commit()

    def get_health(self, node_id: int) -> NodeHealth | None:
        row = self._conn.execute(
            "SELECT * FROM node_health WHERE node_id = ?", (node_id,)).fetchone()
        return _row_to_health(row) if row else None

    def list_health(self) -> list[NodeHealth]:
        rows = self._conn.execute("SELECT * FROM node_health ORDER BY node_id").fetchall()
        return [_row_to_health(r) for r in rows]
