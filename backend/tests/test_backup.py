import threading
import time

import pytest
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.models import Node, Subscription, TuningProfile, RoutingRule, NodeHealth
from pi_gw_panel.backup import export_state, import_state, validate_document, BACKUP_SCHEMA


def _store(path):
    conn = connect(str(path / "t.sqlite"), check_same_thread=False)
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


def test_backup_roundtrips_security_and_network_intent(tmp_path):
    source = _store(tmp_path)
    _populate(source)
    overrides = {
        "kill_switch_enabled": "1", "lan_access_enabled": "0",
        "segment_iface": "eth1.20", "segment_ip": "10.20.0.1",
        "dhcp_start": "10.20.0.30", "dhcp_end": "10.20.0.200",
        "dhcp_lease": "6h", "client_dns": "9.9.9.9",
    }
    for key, value in overrides.items():
        source.set_setting(key, value)
    document = export_state(source)
    assert all(document["settings"][key] == value for key, value in overrides.items())
    target = _store(tmp_path / "network-target")
    import_state(target, document)
    assert {key: target.get_setting(key) for key in overrides} == overrides


def test_restore_canonicalizes_boolean_settings(tmp_path):
    source = _store(tmp_path)
    _populate(source)
    document = export_state(source)
    document["settings"].update({
        "manage_segment": True,
        "kill_switch_enabled": True,
        "lan_access_enabled": False,
    })

    target = _store(tmp_path / "boolean-target")
    import_state(target, document)

    assert target.get_setting("manage_segment") == "1"
    assert target.get_setting("kill_switch_enabled") == "1"
    assert target.get_setting("lan_access_enabled") == "0"


def test_schema2_rejects_empty_profiles(tmp_path):
    store = _store(tmp_path)
    document = {
        "schema_version": 2,
        "nodes": [],
        "subscriptions": [],
        "profiles": [],
        "routing": {"rules": [], "default_action": "proxy"},
        "settings": {},
    }

    with pytest.raises(ValueError, match="profiles"):
        import_state(store, document)


def test_preflight_rejects_secret_key_and_bad_reference_before_delete(tmp_path):
    store = _store(tmp_path)
    _populate(store)
    document = export_state(store)
    document["settings"]["auth_password_hash"] = "pwned"
    document["nodes"][0]["tuning_profile_id"] = 999999
    before = [node.id for node in store.list_nodes()]
    with pytest.raises(ValueError):
        validate_document(document)
    with pytest.raises(ValueError):
        import_state(store, document)
    assert [node.id for node in store.list_nodes()] == before
    assert store.get_setting("auth_password_hash") is None


def test_export_holds_one_snapshot_lock_across_all_tables(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _populate(store)
    entered = threading.Event()
    release = threading.Event()
    writer_done = threading.Event()
    original = store.list_nodes

    def paused_nodes():
        entered.set()
        assert release.wait(timeout=2)
        return original()

    monkeypatch.setattr(store, "list_nodes", paused_nodes)
    exported = {}
    exporter = threading.Thread(target=lambda: exported.update(export_state(store)))
    exporter.start()
    assert entered.wait(timeout=2)

    def writer():
        store.set_setting("health_interval", "999")
        writer_done.set()

    concurrent = threading.Thread(target=writer)
    concurrent.start()
    time.sleep(0.05)
    assert not writer_done.is_set(), "writer must not interleave with the exported snapshot"
    release.set()
    exporter.join(timeout=2)
    concurrent.join(timeout=2)
    assert writer_done.is_set()
    assert exported["settings"]["health_interval"] == "15"
