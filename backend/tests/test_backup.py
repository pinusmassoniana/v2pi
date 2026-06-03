import pytest
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.models import Node, Subscription, TuningProfile, RoutingRule, NodeHealth
from pi_gw_panel.backup import export_state, import_state, BACKUP_SCHEMA


def _store(path):
    conn = connect(str(path / "t.sqlite"))
    init_schema(conn)
    return NodeStore(conn)


def _populate(s):
    pid = s.add_profile(TuningProfile(id=None, name="frag", frag_enabled=True, quic="drop"))
    sid = s.add_subscription(Subscription(id=None, name="sub", url="https://x/y", interval_sec=3600))
    nid = s.add_node(Node(id=None, name="n1", address="1.2.3.4", port=443, uuid="u1",
                          subscription_id=sid, tuning_profile_id=pid))
    s.replace_routing([RoutingRule(id=None, position=0, type="geoip", value="ru", action="direct")])
    s.set_setting("routing_default_action", "direct")
    s.set_setting("health_interval", "15")
    s.set_default_profile(pid)
    # transient state — must NOT be carried in a backup
    s.set_setting("active_node_id", str(nid))
    s.upsert_health(NodeHealth(node_id=nid, last_tcp_ok=True, fail_count=2))
    return pid, sid, nid


def test_export_excludes_transient_and_secrets(tmp_path):
    s = _store(tmp_path)
    _populate(s)
    s.set_setting("auth_username", "admin")
    s.set_setting("auth_password_hash", "salt$hash")
    doc = export_state(s)
    assert doc["schema_version"] == BACKUP_SCHEMA
    assert doc["nodes"][0]["name"] == "n1"
    assert doc["routing"]["default_action"] == "direct"
    assert "active_node_id" not in doc["settings"]
    assert "auth_username" not in doc["settings"] and "auth_password_hash" not in doc["settings"]


def test_backup_restore_roundtrip(tmp_path):
    src = _store(tmp_path)
    pid, sid, nid = _populate(src)
    doc = export_state(src)

    dst = _store(tmp_path / "dst")          # fresh DB (only the seeded default profile)
    summary = import_state(dst, doc)
    assert summary["nodes"] == 1 and summary["profiles"] >= 1

    n = dst.get_node(nid)
    assert n.name == "n1" and n.subscription_id == sid and n.tuning_profile_id == pid
    assert dst.get_profile(pid).frag_enabled is True
    assert [r.type for r in dst.get_routing()] == ["geoip"]
    assert dst.get_setting("routing_default_action") == "direct"
    assert dst.get_setting("health_interval") == "15"
    assert dst.get_default_profile().id == pid
    # transient NOT restored
    assert dst.get_setting("active_node_id") is None
    assert dst.get_health(nid) is None


def test_restore_rejects_bad_schema(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError):
        import_state(s, {"schema_version": 999, "nodes": [], "subscriptions": [],
                         "profiles": [], "routing": {"rules": [], "default_action": "proxy"},
                         "settings": {}})
