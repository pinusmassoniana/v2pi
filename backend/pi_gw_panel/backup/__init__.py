"""Versioned JSON backup/restore of the config-defining state (Wave 3a).

Includes nodes, subscriptions, tuning profiles, routing, and whitelisted settings.
Excludes transient state (node_health, active/prev active-node, last_failover_at) and
secrets (auth_username/auth_password_hash). Restore preserves ids so all references
(subscription_id / tuning_profile_id / default_profile_id) stay valid, in one
transaction. The live xray config regenerates on the next apply (not restored)."""
import json
from pi_gw_panel.config import SETTINGS_DEFAULTS

BACKUP_SCHEMA = 1

# settings carried in a backup: the config knobs + the default-profile pointer.
# Deliberately omits active_node_id / prev_active_node_id / last_failover_at / auth_*.
_SETTINGS_KEYS = list(SETTINGS_DEFAULTS) + ["default_profile_id"]


def _node_dict(n) -> dict:
    return {"id": n.id, "name": n.name, "address": n.address, "port": n.port, "uuid": n.uuid,
            "transport": n.transport, "sni": n.sni, "public_key": n.public_key,
            "short_id": n.short_id, "fingerprint": n.fingerprint, "flow": n.flow,
            "subscription_id": n.subscription_id, "stale": n.stale,
            "tuning_profile_id": n.tuning_profile_id}


def _profile_dict(p) -> dict:
    return {"id": p.id, "name": p.name, "fingerprint": p.fingerprint,
            "frag_enabled": p.frag_enabled, "frag_packets": p.frag_packets,
            "frag_length": p.frag_length, "frag_interval": p.frag_interval,
            "mux_enabled": p.mux_enabled, "doh_enabled": p.doh_enabled,
            "doh_url": p.doh_url, "quic": p.quic}


def export_state(store) -> dict:
    settings = {k: store.get_setting(k) for k in _SETTINGS_KEYS if store.get_setting(k) is not None}
    return {
        "schema_version": BACKUP_SCHEMA,
        "nodes": [_node_dict(n) for n in store.list_nodes()],
        "subscriptions": [{"id": s.id, "name": s.name, "url": s.url,
                           "injection": s.injection, "interval_sec": s.interval_sec}
                          for s in store.list_subscriptions()],
        "profiles": [_profile_dict(p) for p in store.list_profiles()],
        "routing": {
            "rules": [{"type": r.type, "value": r.value, "action": r.action}
                      for r in store.get_routing()],
            "default_action": store.get_setting("routing_default_action") or "proxy",
        },
        "settings": settings,
    }


def import_state(store, doc: dict) -> dict:
    """Transactional snapshot restore (ids preserved). Raises ValueError on a schema
    mismatch. Does not auto-apply — the live config regenerates on the next apply."""
    if doc.get("schema_version") != BACKUP_SCHEMA:
        raise ValueError(f"unsupported backup schema_version: {doc.get('schema_version')!r}")
    conn = store._conn
    with conn:                                          # one transaction (rolls back on error)
        for table in ("node_health", "nodes", "subscriptions", "routing_rules", "tuning_profiles"):
            conn.execute(f"DELETE FROM {table}")
        for p in doc["profiles"]:
            conn.execute(
                "INSERT INTO tuning_profiles (id,name,fingerprint,frag_enabled,frag_packets,"
                "frag_length,frag_interval,mux_enabled,doh_enabled,doh_url,quic) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (p["id"], p["name"], p["fingerprint"], int(p["frag_enabled"]), p["frag_packets"],
                 p["frag_length"], p["frag_interval"], int(p["mux_enabled"]),
                 int(p["doh_enabled"]), p["doh_url"], p["quic"]))
        for s in doc["subscriptions"]:
            conn.execute(
                "INSERT INTO subscriptions (id,name,url,injection_json,interval_sec) VALUES (?,?,?,?,?)",
                (s["id"], s["name"], s["url"], json.dumps(s["injection"]), s["interval_sec"]))
        for n in doc["nodes"]:
            conn.execute(
                "INSERT INTO nodes (id,name,address,port,uuid,transport,sni,public_key,short_id,"
                "fingerprint,flow,subscription_id,stale,tuning_profile_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (n["id"], n["name"], n["address"], n["port"], n["uuid"], n["transport"], n["sni"],
                 n["public_key"], n["short_id"], n["fingerprint"], n["flow"],
                 n["subscription_id"], int(n["stale"]), n["tuning_profile_id"]))
        for i, r in enumerate(doc["routing"]["rules"]):
            conn.execute("INSERT INTO routing_rules (position,type,value,action) VALUES (?,?,?,?)",
                         (i, r["type"], r["value"], r["action"]))
        conn.execute(
            "INSERT INTO settings(key,value) VALUES('routing_default_action',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (doc["routing"]["default_action"],))
        for k, v in doc.get("settings", {}).items():
            conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
    return {"nodes": len(doc["nodes"]), "subscriptions": len(doc["subscriptions"]),
            "profiles": len(doc["profiles"]), "routing_rules": len(doc["routing"]["rules"])}
