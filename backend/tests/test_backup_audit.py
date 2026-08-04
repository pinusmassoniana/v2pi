"""Backup/restore is a second, quieter write path into every setting the UI owns.

Each test here pins one way it used to accept what the interactive API refuses — up to and
including a value that becomes a root-run dnsmasq directive.
"""
import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

import pi_gw_panel.backup as backup_mod
from pi_gw_panel.app import create_app
from pi_gw_panel.backup import (_PRE_RESTORE_RETAIN, export_state, import_state,
                                validate_document, write_pre_restore_snapshot)
from pi_gw_panel.backup.scheduler import BackupScheduler
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.models import Node, TuningProfile
from pi_gw_panel.net_control.plan import NetPlan
from pi_gw_panel.net_control.render import render_dnsmasq
from pi_gw_panel.nodes.store import NodeStore
from conftest import _build_dryrun_state, _login


def _store(path):
    connection = connect(str(path), check_same_thread=False)
    init_schema(connection)
    return NodeStore(connection)


def _document(store=None, **settings):
    """A minimal valid backup document, optionally exported from a live store."""
    if store is not None:
        document = export_state(store)
    else:
        document = {"schema_version": 2, "nodes": [], "subscriptions": [],
                    "profiles": [{"id": 1, "name": "default"}],
                    "routing": {"rules": [], "default_action": "proxy"}, "settings": {}}
    document["settings"].update(settings)
    return document


def _client(settings, stub_xray):
    state = _build_dryrun_state(settings, stub_xray)
    client = TestClient(create_app(settings, state=state))
    headers = {"X-CSRF-Token": _login(client)}
    return client, state, headers


# --- H6: a restored setting is a dnsmasq directive -------------------------------------------

@pytest.mark.parametrize("field, value", [
    ("dhcp_lease", "12h\ndhcp-script=/data/x.sh"),
    ("dhcp_lease", "12h\rdhcp-script=/data/x.sh"),
    ("client_dns6", "2606:4700:4700::1111\ndhcp-script=/data/x.sh"),
    ("segment_iface", "eth0.2\ninterface=eth0"),
])
def test_restore_refuses_a_dnsmasq_directive_smuggled_into_a_setting(tmp_path, field, value):
    """`dhcp_lease` and `client_dns6` are interpolated verbatim into the dnsmasq config the panel
    supervises as root, and the file's line structure IS its syntax. A newline is a new directive
    (`dhcp-script=` runs on the next lease event) that `dnsmasq --test` calls perfectly valid."""
    store = _store(tmp_path / "t.sqlite")
    with pytest.raises(ValueError, match=field):
        validate_document(_document(**{field: value}))
    with pytest.raises(ValueError, match=field):
        import_state(store, _document(**{field: value}))
    assert store.get_setting(field) is None


def test_render_dnsmasq_refuses_a_line_break_on_its_own(settings):
    """Defence in depth: even reached directly, the renderer must not emit an injected directive."""
    plan = NetPlan.from_settings(settings)
    plan.dhcp_lease = "12h\ndhcp-script=/data/x.sh"
    with pytest.raises(ValueError, match="dhcp_lease"):
        render_dnsmasq(plan)
    plan.dhcp_lease = "12h"
    assert "dhcp-script" not in render_dnsmasq(plan)


def test_put_network_refuses_an_embedded_newline(settings, stub_xray):
    """The interactive path had the same hole restore did — `re.match(r'…$', 'eth0\\n')` succeeds —
    and both now go through one fullmatch-based validator. Trailing whitespace normalizes away;
    a newline with anything after it is an injected directive and is refused."""
    client, state, headers = _client(settings, stub_xray)
    for field, value in (("segment_iface", "eth0.2\ninterface=eth0"),
                         ("dhcp_lease", "12h\ndhcp-script=/data/x.sh"),
                         ("client_dns6", "2606:4700:4700::1111\nlog-queries")):
        response = client.put("/api/network", json={field: value}, headers=headers)
        assert response.status_code == 422, field
        assert state.store.get_setting(field) is None
    assert client.put("/api/network", json={"segment_iface": "eth0.2\n"},
                      headers=headers).status_code == 200
    assert state.store.get_setting("segment_iface") == "eth0.2"


def test_restore_refuses_road_warrior_values_the_api_would_refuse(tmp_path):
    """C2's validators, reused: a backup could carry any string under 2048 chars into
    realitySettings and into the generated client .conf / vless:// link."""
    store = _store(tmp_path / "t.sqlite")
    for field, value in (("rw_dest", "not a host:port"),
                         ("rw_public_key", "short"),
                         ("rw_endpoint", "gateway.example.com\nFINAL,DIRECT"),
                         ("rw_server_names", "www.example.com,bad name"),
                         ("rw_short_ids", "zzz"),
                         ("rw_port", "70000"),
                         ("rw_routed_nets", "10.0.0.0/99")):
        with pytest.raises(ValueError):
            import_state(store, _document(**{field: value}))
        assert store.get_setting(field) is None


# --- F5-7: two copies of the default action, one of them unchecked ---------------------------

def test_settings_copy_cannot_override_the_validated_default_action(tmp_path):
    """The raw settings loop ran AFTER the Literal-checked insert and won, so `settings` could
    put any string into the routing default that /api/settings and /api/routing both refuse."""
    store = _store(tmp_path / "t.sqlite")
    document = _document(routing_default_action="everything-through-the-void")
    with pytest.raises(ValueError, match="routing_default_action"):
        import_state(store, document)
    assert store.get_setting("routing_default_action") is None

    conflicting = _document(routing_default_action="block")
    conflicting["routing"]["default_action"] = "proxy"
    with pytest.raises(ValueError, match="conflict"):
        import_state(store, conflicting)

    agreeing = _document(routing_default_action="block")
    agreeing["routing"]["default_action"] = "block"
    import_state(store, agreeing)
    assert store.get_setting("routing_default_action") == "block"


def test_a_document_without_a_routing_block_default_adopts_the_settings_copy(tmp_path):
    store = _store(tmp_path / "t.sqlite")
    document = _document(routing_default_action="direct")
    document["routing"] = {"rules": []}          # older documents may omit it
    import_state(store, document)
    assert store.get_setting("routing_default_action") == "direct"


# --- F1-1: a restored non-number used to 500 the settings screen for good --------------------

def test_restore_refuses_a_non_numeric_setting(settings, stub_xray):
    client, state, headers = _client(settings, stub_xray)
    document = client.get("/api/backup").json()
    document["settings"]["health_interval"] = "not-a-number"
    response = client.post("/api/restore", json=document, headers=headers)
    assert response.status_code == 400
    assert "health_interval" in response.json()["detail"]
    assert client.get("/api/settings").status_code == 200


def test_settings_survive_an_already_poisoned_value(settings, stub_xray):
    """A DB poisoned by an older build (or by hand) must not lock the operator out of the one
    screen that can repair it — the value falls back to its default until it is rewritten."""
    client, state, headers = _client(settings, stub_xray)
    state.store.set_setting("health_interval", "not-a-number")
    body = client.get("/api/settings")
    assert body.status_code == 200 and body.json()["health_interval"] == 1800
    assert client.put("/api/settings", json={"health_interval": 600},
                      headers=headers).status_code == 200
    assert client.get("/api/settings").json()["health_interval"] == 600


def test_settings_bounds_still_bite_on_the_interactive_path(settings, stub_xray):
    client, _state, headers = _client(settings, stub_xray)
    assert client.put("/api/settings", json={"health_interval": 5},
                      headers=headers).status_code == 422
    assert client.put("/api/settings", json={"stats_api_port": 52345},
                      headers=headers).status_code == 422


# --- F5-3: remote access restored onto a box that holds no key -------------------------------

def test_restore_cannot_claim_remote_access_is_on_without_the_private_key(settings, stub_xray):
    """`rw_private_key` deliberately never travels in a backup, so an `rw_enabled=1` document
    would leave /api/rw reporting the inbound as enabled while resolve() returns None and
    nothing is served — a split state PUT /api/rw itself refuses to create."""
    client, state, headers = _client(settings, stub_xray)
    document = client.get("/api/backup").json()
    document["settings"]["rw_enabled"] = "1"
    document["settings"]["rw_public_key"] = "J" * 43

    response = client.post("/api/restore", json=document, headers=headers)

    assert response.status_code == 200
    assert response.json()["restored"]["rw_disabled"]
    assert state.store.get_setting("rw_enabled") == "0"
    assert client.get("/api/rw").json()["enabled"] is False


def test_restore_disables_remote_access_when_the_public_key_is_not_the_local_pair(
        settings, stub_xray):
    client, state, headers = _client(settings, stub_xray)
    state.store.set_setting("rw_private_key", "A" * 43)
    state.store.set_setting("rw_public_key", "B" * 43)
    document = client.get("/api/backup").json()
    document["settings"]["rw_enabled"] = "1"
    document["settings"]["rw_public_key"] = "C" * 43      # another gateway's half

    assert client.post("/api/restore", json=document, headers=headers).status_code == 200
    assert state.store.get_setting("rw_enabled") == "0"


def test_restore_keeps_remote_access_on_for_the_matching_pair(settings, stub_xray):
    client, state, headers = _client(settings, stub_xray)
    state.store.set_setting("rw_private_key", "A" * 43)
    state.store.set_setting("rw_public_key", "B" * 43)
    document = client.get("/api/backup").json()
    document["settings"]["rw_enabled"] = "1"
    document["settings"]["rw_public_key"] = "B" * 43

    assert client.post("/api/restore", json=document, headers=headers).status_code == 200
    assert state.store.get_setting("rw_enabled") == "1"


# --- F5-4: what the API can store, a backup must be able to carry ----------------------------

def test_the_api_cannot_store_a_node_a_backup_could_not_carry(settings, stub_xray):
    """An empty name was storable through the API and refused by the backup schema, so a single
    such node made EVERY later backup unrestorable — discovered only during a recovery."""
    client, state, headers = _client(settings, stub_xray)
    for body in ({"name": "", "address": "7.7.7.7", "port": 443, "uuid": "u"},
                 {"name": "n", "address": "", "port": 443, "uuid": "u"},
                 {"name": "n", "address": "7.7.7.7", "port": 443, "uuid": ""}):
        assert client.post("/api/nodes", json=body, headers=headers).status_code == 422
    created = client.post("/api/nodes",
                          json={"name": "n", "address": "7.7.7.7", "port": 443, "uuid": "u"},
                          headers=headers)
    assert created.status_code == 200
    assert client.patch(f"/api/nodes/{created.json()['id']}", json={"name": ""},
                        headers=headers).status_code == 422
    validate_document(client.get("/api/backup").json())     # must not raise


def test_auto_backup_refuses_to_write_a_file_restore_would_reject(settings, tmp_path):
    """A daily file that cannot be restored is worse than no file: it looks like a safety net
    until the day it is needed. Drift has to surface on the first run after it appears."""
    class _State:
        pass

    state = _State()
    state.settings = settings
    state.store = _store(tmp_path / "sched.sqlite")
    state.store.set_setting("auto_backup_enabled", "1")
    state.store.add_profile(TuningProfile(id=None, name="p"))
    path = BackupScheduler(state).run_once(now=1000)
    with open(path) as handle:
        validate_document(json.load(handle))

    state.store.add_node(Node(id=None, name="", address="7.7.7.7", port=443, uuid="u"))
    with pytest.raises(ValueError):
        BackupScheduler(state).run_once(now=2000)
    assert not os.path.exists(os.path.join(os.path.dirname(path), "backup-2000.json"))


def test_get_backup_reports_unrestorable_state_instead_of_handing_it_over(settings, stub_xray):
    client, state, headers = _client(settings, stub_xray)
    state.store.add_node(Node(id=None, name="", address="7.7.7.7", port=443, uuid="u"))
    response = client.get("/api/backup")
    assert response.status_code == 500 and "not restorable" in response.json()["detail"]


# --- F5-6: restore is a destructive whole-state replace --------------------------------------

def test_restore_snapshots_what_it_replaces(settings, stub_xray):
    client, state, headers = _client(settings, stub_xray)
    keep = client.post("/api/nodes",
                       json={"name": "before", "address": "7.7.7.7", "port": 443, "uuid": "u"},
                       headers=headers).json()["id"]
    replacement = client.get("/api/backup").json()
    replacement["nodes"] = []

    response = client.post("/api/restore", json=replacement, headers=headers)

    assert response.status_code == 200
    snapshot = response.json()["pre_restore_snapshot"]
    assert os.path.isfile(snapshot)
    assert stat.S_IMODE(os.stat(snapshot).st_mode) == 0o600
    assert client.get("/api/nodes").json() == []
    with open(snapshot) as handle:
        saved = json.load(handle)
    assert [node["id"] for node in saved["nodes"]] == [keep]
    # and it is a real backup: restoring it undoes the restore
    assert client.post("/api/restore", json=saved, headers=headers).status_code == 200
    assert [node["name"] for node in client.get("/api/nodes").json()] == ["before"]


def test_pre_restore_snapshot_lands_in_the_backups_dir(settings, tmp_path):
    class _State:
        pass

    state = _State()
    state.settings = settings
    state.store = _store(tmp_path / "snap.sqlite")
    state.store.add_profile(TuningProfile(id=None, name="p"))
    path = write_pre_restore_snapshot(state, now=1234)
    directory = os.path.join(settings.data_dir, "backups")
    # Filename carries a random suffix (audit FIX-E-4) so two restores in the same second can
    # never collide onto one path — assert the stable parts, not the exact name.
    assert os.path.dirname(path) == directory
    name = os.path.basename(path)
    assert name.startswith("pre-restore-1234-") and name.endswith(".json")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700


def test_pre_restore_snapshots_do_not_collide_within_the_same_second(settings, tmp_path):
    """FIX-E-4: two restores landing in the same wall-clock second used to resolve to the
    identical `pre-restore-<ts>.json` path — the second write's `os.replace` then silently
    overwrote the first recovery copy, exactly when the safety net mattered most."""
    class _State:
        pass

    state = _State()
    state.settings = settings
    state.store = _store(tmp_path / "snap.sqlite")
    state.store.add_profile(TuningProfile(id=None, name="p"))

    first = write_pre_restore_snapshot(state, now=1234)
    second = write_pre_restore_snapshot(state, now=1234)

    assert first != second
    assert os.path.isfile(first) and os.path.isfile(second)
    with open(first) as handle:
        json.load(handle)
    with open(second) as handle:
        json.load(handle)


def test_pre_restore_snapshots_are_pruned_to_a_bounded_count(settings, tmp_path):
    """A burst of restores must not grow the backups directory without bound."""
    class _State:
        pass

    state = _State()
    state.settings = settings
    state.store = _store(tmp_path / "snap.sqlite")
    state.store.add_profile(TuningProfile(id=None, name="p"))

    for i in range(_PRE_RESTORE_RETAIN + 5):
        write_pre_restore_snapshot(state, now=1000 + i)

    directory = os.path.join(settings.data_dir, "backups")
    kept = [f for f in os.listdir(directory) if f.startswith("pre-restore-")]
    assert len(kept) == _PRE_RESTORE_RETAIN


def test_failed_pre_restore_snapshot_does_not_leave_an_empty_reservation(
        settings, tmp_path, monkeypatch):
    """FIX-J-4: `_reserve_snapshot_path` claims the FINAL path itself as an empty 0-byte
    placeholder (so two concurrent restores can never collide on one filename); `write_document`
    normally fills it in via `os.replace`. If serialization/fsync on the temp file fails first,
    the empty reservation used to survive at that path — and the pruner counts any
    `pre-restore-*.json` file toward the retention cap by mtime, valid or not, so the junk file
    could later displace a real, older snapshot. Force the write to fail and confirm no empty
    file is left behind and the pre-existing valid snapshot is untouched."""
    class _State:
        pass

    state = _State()
    state.settings = settings
    state.store = _store(tmp_path / "snap.sqlite")
    state.store.add_profile(TuningProfile(id=None, name="p"))

    good = write_pre_restore_snapshot(state, now=1000)   # one valid, older snapshot
    directory = os.path.join(settings.data_dir, "backups")
    assert os.path.isfile(good)

    def failing_write_document(doc, path):
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(backup_mod, "write_document", failing_write_document)

    with pytest.raises(OSError, match="simulated fsync failure"):
        write_pre_restore_snapshot(state, now=1001)

    names = os.listdir(directory)
    assert os.path.basename(good) in names               # the valid, older snapshot survives
    assert not [f for f in names if "1001" in f]          # no empty reservation left behind
    for f in names:
        assert os.path.getsize(os.path.join(directory, f)) > 0

    monkeypatch.undo()
    # the store must still be usable for a normal snapshot afterwards
    later = write_pre_restore_snapshot(state, now=1002)
    assert os.path.isfile(later) and os.path.getsize(later) > 0
