"""A8: the backup documents the gateway already holds — listed, downloadable, and undoable.

The pieces existed before this: the daily job writes `backup-<ts>.json`, every restore writes a
`pre-restore-<ts>-<uuid>.json` copy of what it is about to replace. None of them were readable
from the panel, so the safety net was invisible and the undo was "ssh in and find the file".
"""
import json
import os

from pi_gw_panel import backup as backup_mod
from pi_gw_panel.backup.scheduler import BackupScheduler
from tests.conftest import _build_dryrun_state, _client, _login

from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app


def _app(settings, stub_xray):
    state = _build_dryrun_state(settings, stub_xray)
    client = TestClient(create_app(settings, state=state))
    return client, state, {"X-CSRF-Token": _login(client)}


def _node(client, headers, name):
    return client.post("/api/nodes", json={"name": name, "address": "7.7.7.7", "port": 443,
                                           "uuid": f"u-{name}"}, headers=headers).json()["id"]


# --- the name pattern is the whole path-traversal surface ---

def test_document_kind_accepts_only_the_two_names_this_package_writes():
    assert backup_mod.document_kind("backup-1700000000.json") == ("auto", 1700000000)
    assert backup_mod.document_kind(f"pre-restore-1700000000-{'a' * 32}.json") == (
        "pre-restore", 1700000000)
    for hostile in ("../../etc/passwd", "backup-1700000000.json/../x", "backup-.json",
                    "backup-1700000000.json.bak", "pre-restore-1700000000.json",
                    f"pre-restore-1700000000-{'A' * 32}.json", "xray.json", ""):
        assert backup_mod.document_kind(hostile) is None, hostile


def test_read_document_refuses_a_name_it_did_not_write(settings):
    outside = os.path.join(settings.data_dir, "secret.json")
    with open(outside, "w") as handle:
        json.dump({"schema_version": 2}, handle)
    for name in ("../secret.json", "secret.json"):
        try:
            backup_mod.read_document(settings, name)
        except KeyError:
            continue
        raise AssertionError(f"{name} was read")


# --- listing ---

def test_list_documents_is_newest_first_and_skips_what_is_not_restorable(settings):
    directory = backup_mod.backups_dir(settings)
    for name in (f"pre-restore-1700000100-{'b' * 32}.json", "backup-1700000000.json"):
        backup_mod.write_document({"schema_version": 2}, os.path.join(directory, name))
    # A reservation whose write never landed: `_reserve_snapshot_path` claims the name first.
    open(os.path.join(directory, f"pre-restore-1700000200-{'c' * 32}.json"), "w").close()
    open(os.path.join(directory, "notes.txt"), "w").write("hello")

    rows = backup_mod.list_documents(settings)

    assert [row["name"] for row in rows] == [f"pre-restore-1700000100-{'b' * 32}.json",
                                             "backup-1700000000.json"]
    assert [row["kind"] for row in rows] == ["pre-restore", "auto"]
    assert [row["created_at"] for row in rows] == [1700000100, 1700000000]
    assert all(row["bytes"] > 0 for row in rows)


def test_backups_route_lists_the_daily_copy_the_scheduler_wrote(settings, stub_xray):
    client, state, headers = _app(settings, stub_xray)
    state.store.set_setting("auto_backup_enabled", "1")
    BackupScheduler(state).run_once(now=1700000000)

    rows = client.get("/api/backups").json()

    assert rows == [{"name": "backup-1700000000.json", "bytes": rows[0]["bytes"],
                     "created_at": 1700000000, "kind": "auto"}]
    assert rows[0]["bytes"] > 0


def test_backups_need_a_session(settings, stub_xray):
    client = _client(settings, stub_xray)
    assert client.get("/api/backups").status_code == 401
    assert client.get("/api/backups/backup-1700000000.json").status_code == 401


# --- download ---

def test_stored_backup_downloads_and_an_unknown_name_is_404(settings, stub_xray):
    client, state, headers = _app(settings, stub_xray)
    _node(client, headers, "kept")
    state.store.set_setting("auto_backup_enabled", "1")
    BackupScheduler(state).run_once(now=1700000000)

    doc = client.get("/api/backups/backup-1700000000.json").json()

    assert doc["schema_version"] == backup_mod.BACKUP_SCHEMA
    assert [node["name"] for node in doc["nodes"]] == ["kept"]
    # The same document the ordinary restore path takes.
    assert client.post("/api/restore", json=doc, headers=headers).status_code == 200
    assert client.get("/api/backups/backup-1699999999.json").status_code == 404
    assert client.get("/api/backups/..%2F..%2Fetc%2Fpasswd").status_code == 404


# --- undo ---

def test_undo_restores_what_the_last_restore_replaced(settings, stub_xray):
    client, state, headers = _app(settings, stub_xray)
    _node(client, headers, "first")
    document = client.get("/api/backup").json()

    client.delete(f"/api/nodes/{client.get('/api/nodes').json()[0]['id']}", headers=headers)
    _node(client, headers, "second")
    assert client.post("/api/restore", json=document, headers=headers).status_code == 200
    assert [n["name"] for n in client.get("/api/nodes").json()] == ["first"]

    undone = client.post("/api/restore/undo", headers=headers)

    assert undone.status_code == 200
    body = undone.json()
    assert body["runtime"] == "disconnected"
    assert body["undone_from"].startswith("pre-restore-")
    assert [n["name"] for n in client.get("/api/nodes").json()] == ["second"]
    # The undo took a snapshot of its own, so it is itself undoable.
    snapshots = [row for row in client.get("/api/backups").json() if row["kind"] == "pre-restore"]
    assert len(snapshots) == 2
    assert client.post("/api/restore/undo", headers=headers).status_code == 200
    assert [n["name"] for n in client.get("/api/nodes").json()] == ["first"]


def test_undo_with_no_snapshot_says_so_rather_than_500(settings, stub_xray):
    client, _state, headers = _app(settings, stub_xray)
    response = client.post("/api/restore/undo", headers=headers)
    assert response.status_code == 404
    assert "no pre-restore snapshot" in response.json()["detail"]


def test_undo_is_a_write_and_needs_the_csrf_token(settings, stub_xray):
    client, _state, _headers = _app(settings, stub_xray)
    assert client.post("/api/restore/undo").status_code == 403
