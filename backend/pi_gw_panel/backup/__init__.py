"""Versioned JSON backup/restore of the config-defining state.

Includes nodes, subscriptions, tuning profiles, routing, and whitelisted settings.
Excludes transient state (node_health, active/prev active-node, last_failover_at) and
secrets (auth_username/auth_password_hash). Restore preserves ids so all references
(subscription_id / tuning_profile_id / default_profile_id) stay valid, in one
transaction. The live xray config regenerates on the next apply (not restored).

Schema 2 carries the full current column set (node stream fields, all tuning anti-DPI
knobs, subscription enabled/default-profile, routing enable/label). Schema-1 backups still
import — missing fields fall back to defaults, and node network/security are re-derived from
transport/public-key via Node.normalize()."""
import json
from pi_gw_panel.config import SETTINGS_DEFAULTS
from pi_gw_panel.models import Node, TuningProfile
from pi_gw_panel.nodes.store import _NODE_COLS, _node_values, _PROFILE_COLS, _profile_values

BACKUP_SCHEMA = 2

# settings carried in a backup: the config knobs + the default-profile pointer.
# Deliberately omits active_node_id / prev_active_node_id / last_failover_at / auth_*.
_SETTINGS_KEYS = list(SETTINGS_DEFAULTS) + ["default_profile_id"]

# Node attrs to serialize = id + every stored column (kept in sync with the store).
_NODE_DUMP = ("id",) + _NODE_COLS
_PROFILE_DUMP = ("id",) + _PROFILE_COLS + ("noises",)   # noises is the model attr (JSON in DB)


def _node_dict(n: Node) -> dict:
    return {c: getattr(n, c) for c in _NODE_DUMP}


def _profile_dict(p: TuningProfile) -> dict:
    return {c: getattr(p, c) for c in _PROFILE_DUMP}


def export_state(store) -> dict:
    settings = {k: store.get_setting(k) for k in _SETTINGS_KEYS if store.get_setting(k) is not None}
    return {
        "schema_version": BACKUP_SCHEMA,
        "nodes": [_node_dict(n) for n in store.list_nodes()],
        "subscriptions": [{"id": s.id, "name": s.name, "url": s.url, "injection": s.injection,
                           "interval_sec": s.interval_sec, "enabled": s.enabled,
                           "default_profile_id": s.default_profile_id}
                          for s in store.list_subscriptions()],
        "profiles": [_profile_dict(p) for p in store.list_profiles()],
        "routing": {
            "rules": [{"type": r.type, "value": r.value, "action": r.action,
                       "enabled": r.enabled, "label": r.label} for r in store.get_routing()],
            "default_action": store.get_setting("routing_default_action") or "proxy",
        },
        "settings": settings,
    }


def _profile_from(p: dict) -> TuningProfile:
    return TuningProfile(
        id=p["id"], name=p["name"], fingerprint=p.get("fingerprint", "chrome"),
        frag_enabled=bool(p.get("frag_enabled", False)), frag_packets=p.get("frag_packets", "tlshello"),
        frag_length=p.get("frag_length", "100-200"), frag_interval=p.get("frag_interval", "10-20"),
        mux_enabled=bool(p.get("mux_enabled", False)), doh_enabled=bool(p.get("doh_enabled", True)),
        doh_url=p.get("doh_url", ""), quic=p.get("quic", "allow"),
        noise_enabled=bool(p.get("noise_enabled", False)), noises=p.get("noises", []),
        xhttp_padding=p.get("xhttp_padding", ""), xmux_max_concurrency=p.get("xmux_max_concurrency", ""),
        xmux_max_connections=p.get("xmux_max_connections", ""), mux_concurrency=p.get("mux_concurrency", ""),
        xudp_proxy_udp443=p.get("xudp_proxy_udp443", ""), alpn=p.get("alpn", ""),
        tls_min=p.get("tls_min", ""), tls_max=p.get("tls_max", ""))


def _node_from(n: dict) -> Node:
    pbk = n.get("public_key", "")
    # Node.__post_init__ → normalize() re-derives network/security/flow from transport, so a
    # v1 backup (no network/security stored) still restores to a coherent config.
    return Node(
        id=n["id"], name=n["name"], address=n["address"], port=n["port"], uuid=n["uuid"],
        transport=n.get("transport", "vision"), sni=n.get("sni", ""), public_key=pbk,
        short_id=n.get("short_id", ""), fingerprint=n.get("fingerprint", "chrome"),
        flow=n.get("flow", "xtls-rprx-vision"), network=n.get("network", "tcp"),
        security=n.get("security") or ("reality" if pbk else "tls"),
        path=n.get("path", ""), host=n.get("host", ""), mode=n.get("mode", ""), alpn=n.get("alpn", ""),
        subscription_id=n.get("subscription_id"), stale=bool(n.get("stale", False)),
        tuning_profile_id=n.get("tuning_profile_id"), position=n.get("position", 0))


def import_state(store, doc: dict) -> dict:
    """Transactional snapshot restore (ids preserved). Raises ValueError on an unknown schema.
    Does not auto-apply — the live config regenerates on the next apply."""
    if doc.get("schema_version") not in (1, 2):
        raise ValueError(f"unsupported backup schema_version: {doc.get('schema_version')!r}")
    conn = store._conn
    with conn:                                          # one transaction (rolls back on error)
        conn.execute("DELETE FROM node_health")
        conn.execute("DELETE FROM nodes")
        conn.execute("DELETE FROM subscriptions")
        conn.execute("DELETE FROM routing_rules")
        conn.execute("DELETE FROM tuning_profiles")
        for p in doc["profiles"]:
            prof = _profile_from(p)
            cols = ("id",) + _PROFILE_COLS + ("noises_json",)
            vals = (prof.id, *_profile_values(prof), json.dumps(prof.noises))
            conn.execute(f"INSERT INTO tuning_profiles ({', '.join(cols)}) "
                         f"VALUES ({', '.join(['?'] * len(cols))})", vals)
        for s in doc["subscriptions"]:
            conn.execute(
                "INSERT INTO subscriptions (id,name,url,injection_json,interval_sec,enabled,"
                "default_profile_id) VALUES (?,?,?,?,?,?,?)",
                (s["id"], s["name"], s["url"], json.dumps(s["injection"]), s["interval_sec"],
                 int(s.get("enabled", True)), s.get("default_profile_id")))
        for n in doc["nodes"]:
            node = _node_from(n)
            cols = ("id",) + _NODE_COLS
            vals = (node.id, *_node_values(node))
            conn.execute(f"INSERT INTO nodes ({', '.join(cols)}) "
                         f"VALUES ({', '.join(['?'] * len(cols))})", vals)
        for i, r in enumerate(doc["routing"]["rules"]):
            conn.execute(
                "INSERT INTO routing_rules (position,type,value,action,enabled,label) "
                "VALUES (?,?,?,?,?,?)",
                (i, r["type"], r["value"], r["action"], int(r.get("enabled", True)), r.get("label", "")))
        conn.execute(
            "INSERT INTO settings(key,value) VALUES('routing_default_action',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (doc["routing"]["default_action"],))
        for k, v in doc.get("settings", {}).items():
            conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
    return {"nodes": len(doc["nodes"]), "subscriptions": len(doc["subscriptions"]),
            "profiles": len(doc["profiles"]), "routing_rules": len(doc["routing"]["rules"])}
