"""Regression cover for the auth + public-API-surface audit.

Each test here pins a defect that was live in the shipped panel: a password rotation that left
API tokens working, header comparisons that 500'd instead of 403'ing, foreign-key and
uniqueness violations surfacing as 500s, whitespace passing validation, a delete that reported
success for a row that never existed, a partially-applied import, a login guard that wedged at
its cap, and a path parameter the handler ignored.
"""
import asyncio
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from pi_gw_panel.api import deps, routes
from pi_gw_panel.auth.auth import SESSION_AUTHED
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.__main__ import ensure_bootstrap_token
from conftest import _client


_PASSWORD = "changeme123"


# Not delegated to conftest._login: this one asserts the setup call itself succeeds, and
# _PASSWORD is reused below for a password-rotation and a re-setup-409 check, so it can't
# just take the shared helper's default.
def _login(c):
    assert c.post(
        "/api/setup", json={"username": "admin", "password": _PASSWORD}).status_code == 200
    return c.get("/api/csrf").json()["csrf"]


def _node_body(**over):
    body = {"name": "n1", "address": "1.2.3.4", "port": 47000, "uuid": "u-1",
            "sni": "www.microsoft.com", "public_key": "PK", "short_id": "ab12"}
    body.update(over)
    return body


# --- F1-8: a password change must end every credential issued before it ---
def test_password_change_revokes_pre_rotation_api_tokens(settings, stub_xray):
    c = _client(settings, stub_xray)
    csrf = _login(c)
    secret = c.post("/api/tokens", json={"name": "ci", "scope": "readwrite"},
                    headers={"X-CSRF-Token": csrf}).json()["token"]
    bearer = {"Authorization": f"Bearer {secret}"}
    token_client = TestClient(c.app)                        # no session cookie: token auth only
    assert token_client.get("/api/nodes", headers=bearer).status_code == 200
    assert c.post("/api/password",
                  json={"current_password": _PASSWORD, "new_password": "rotated456"},
                  headers={"X-CSRF-Token": csrf}).status_code == 200
    # reads AND writes are both gone — require_auth short-circuits on a Bearer token and never
    # consults the session epoch, so the revocation has to reach the token itself
    assert token_client.get("/api/nodes", headers=bearer).status_code == 401
    assert token_client.post("/api/nodes", json=_node_body(), headers=bearer).status_code == 401
    assert c.get("/api/tokens").json() == []                # and the operator can see they're gone
    # the session that performed the rotation survives it
    assert c.get("/api/status").status_code == 200


# --- F1-3 / F1-4: a non-ASCII header is a failed check, not a crash ---
def test_non_ascii_csrf_header_is_403_not_500(settings, stub_xray):
    c = _client(settings, stub_xray)
    _login(c)
    # Starlette decodes headers as latin-1; compare_digest raises TypeError on the resulting str
    r = c.post("/api/xray/stop", headers={"X-CSRF-Token": b"\xc3wrong"})
    assert r.status_code == 403 and r.json()["detail"] == "bad csrf token"


def test_non_ascii_bootstrap_token_is_403_not_500(settings, stub_xray):
    settings.bind_host = "0.0.0.0"
    settings.tls_cert = "/tmp/test-cert"
    settings.tls_key = "/tmp/test-key"
    ensure_bootstrap_token(settings.data_dir)
    c = _client(settings, stub_xray)
    r = c.post("/api/setup", json={"username": "admin", "password": _PASSWORD},
               headers={"X-Bootstrap-Token": b"\xc3wrong"})
    assert r.status_code == 403 and r.json()["detail"] == "bad bootstrap token"


# --- F1-5: an unknown tuning profile is a 422, not a foreign-key 500 ---
def test_unknown_tuning_profile_is_422_everywhere_it_is_accepted(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid = c.post("/api/nodes", json=_node_body(), headers=h).json()["id"]
    assert c.patch(f"/api/nodes/{nid}", json={"tuning_profile_id": 424242},
                   headers=h).status_code == 422
    assert c.post("/api/subs", json={"name": "s", "url": "https://example.com/sub",
                                     "default_profile_id": 424242},
                  headers=h).status_code == 422
    sid = c.post("/api/subs", json={"name": "s", "url": "https://example.com/sub"},
                 headers=h).json()["id"]
    assert c.patch(f"/api/subs/{sid}", json={"default_profile_id": 424242},
                   headers=h).status_code == 422
    assert c.post("/api/nodes/validate", json=_node_body(tuning_profile_id=424242),
                  headers=h).status_code == 422
    # null still means "inherit the default" and must stay accepted
    assert c.patch(f"/api/nodes/{nid}", json={"tuning_profile_id": None},
                   headers=h).status_code == 200


# --- F2-1: a duplicate identity is a 409, not a 500 ---
def test_duplicate_node_identity_is_409(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    assert c.post("/api/nodes", json=_node_body(), headers=h).status_code == 200
    r = c.post("/api/nodes", json=_node_body(name="same-server-again"), headers=h)
    assert r.status_code == 409
    other = c.post("/api/nodes", json=_node_body(uuid="u-2"), headers=h).json()["id"]
    # editing one node onto another's identity conflicts the same way
    assert c.patch(f"/api/nodes/{other}", json={"uuid": "u-1"}, headers=h).status_code == 409


# --- F1-7: a whitespace-only net setting is rejected, never stored ---
def test_blank_net_setting_is_rejected(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    store = c.app.state.app_state.store
    before = c.get("/api/network").json()["segment"]["ip"]
    for field in ("segment_ip", "segment_iface", "client_dns", "dhcp_lease", "dhcp_start"):
        r = c.put("/api/network", json={field: "  "}, headers=h)
        assert r.status_code == 422, field
        assert not store.get_setting(field), field       # nothing whitespace-y was written
    assert c.get("/api/network").json()["segment"]["ip"] == before
    # segment_ip6 is genuinely clearable, so a blank one normalizes to empty instead of 422
    assert c.put("/api/network", json={"segment_ip6": "  "}, headers=h).status_code == 200
    assert store.get_setting("segment_ip6") == ""
    # a padded but valid value is stored stripped, not verbatim
    assert c.put("/api/network", json={"segment_ip": " 10.9.9.1 "}, headers=h).status_code == 200
    assert store.get_setting("segment_ip") == "10.9.9.1"


# --- F1-10: deleting a subscription that isn't there is a 404 ---
def test_delete_missing_subscription_is_404(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    assert c.delete("/api/subs/424242", headers=h).status_code == 404
    sid = c.post("/api/subs", json={"name": "s", "url": "https://example.com/sub"},
                 headers=h).json()["id"]
    assert c.delete(f"/api/subs/{sid}", headers=h).status_code == 200
    assert c.delete(f"/api/subs/{sid}", headers=h).status_code == 404   # gone the second time


# --- F1-11 / F2-7: an import is all-or-nothing, and its strings are bounded ---
def test_import_failure_persists_nothing(settings, stub_xray, monkeypatch):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    store = c.app.state.app_state.store
    text = "[" + ",".join(
        f'{{"name":"i{i}","address":"5.5.5.{i}","port":443,"uuid":"u{i}"}}'
        for i in range(5)) + "]"
    real_add = store.add_node
    calls = {"n": 0}

    def failing_add(node):
        calls["n"] += 1
        if calls["n"] == 3:
            raise sqlite3.IntegrityError("simulated constraint failure")
        return real_add(node)

    monkeypatch.setattr(store, "add_node", failing_add)
    assert c.post("/api/nodes/import", json={"text": text}, headers=h).status_code == 409
    monkeypatch.undo()
    assert store.list_nodes() == []          # the two that "succeeded" were rolled back too


def test_import_clamps_untrusted_node_strings(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    text = ('[{"name":"' + "n" * 4000 + '","address":"5.5.5.9","port":443,'
            '"uuid":"' + "u" * 4000 + '"}]')
    assert c.post("/api/nodes/import", json={"text": text}, headers=h).json()["added"] == 1
    node = c.get("/api/nodes").json()[0]
    assert len(node["name"]) == 256 and len(node["uuid"]) == 128


# --- F1-12: a full login-guard table must not blanket-429 every untracked client ---
def test_login_guard_evicts_lru_instead_of_wedging(settings, stub_xray):
    c = _client(settings, stub_xray)
    _login(c)
    guards = c.app.state.login_guard
    guards.clear()
    # every bucket mid-count (1..4): these have until == 0.0 and were never prunable, so the
    # table used to stay at the cap until the process restarted
    for i in range(routes._LOGIN_GUARD_MAX):
        guards[f"10.0.{i // 256}.{i % 256}"] = {
            "count": 1, "until": 0.0, "in_flight": 0, "seen": 1000.0 + i}
    r = c.post("/api/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401                      # reached the credential check, not a 429
    assert "10.0.0.0" not in guards                  # the least-recently-seen bucket made room
    assert "testclient" in guards
    assert len(guards) <= routes._LOGIN_GUARD_MAX


# --- dead code: the disconnect path parameter is enforced, not discarded ---
def test_disconnect_rejects_a_node_that_is_not_connected(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    active = c.post("/api/nodes", json=_node_body(), headers=h).json()["id"]
    other = c.post("/api/nodes", json=_node_body(uuid="u-2"), headers=h).json()["id"]
    assert c.post(f"/api/nodes/{active}/disconnect", headers=h).status_code == 409  # none active
    assert c.post(f"/api/nodes/{active}/apply", headers=h).status_code == 200
    r = c.post(f"/api/nodes/{other}/disconnect", headers=h)
    assert r.status_code == 409                       # must NOT silently take down `active`
    assert c.get("/api/status").json()["active_node_id"] == active
    assert c.post(f"/api/nodes/{active}/disconnect", headers=h).status_code == 200
    assert c.get("/api/status").json()["active_node_id"] is None


# --- F1-6 (panel side): the WS session check must not run on the event loop ---
def test_session_check_async_runs_off_the_event_loop(settings):
    conn = connect(":memory:", check_same_thread=False)
    init_schema(conn)
    store = NodeStore(conn)
    threads = {}

    class _RecordingStore:
        def get_setting(self, key):
            threads["store"] = threading.get_ident()
            return store.get_setting(key)

    async def run():
        threads["loop"] = threading.get_ident()
        return await deps.session_invalid_reason_async({SESSION_AUTHED: True}, _RecordingStore())

    assert asyncio.run(run()) is None
    assert threads["store"] != threads["loop"], "blocking store I/O stayed on the event loop"


def test_session_check_async_matches_the_sync_contract(settings):
    conn = connect(":memory:", check_same_thread=False)
    init_schema(conn)
    store = NodeStore(conn)
    assert asyncio.run(deps.session_invalid_reason_async({}, store)) == "auth required"
    store.set_setting("session_epoch", "4")
    assert asyncio.run(
        deps.session_invalid_reason_async({SESSION_AUTHED: True}, store)) == "session expired"


# --- dead code: the scope vocabulary has one definition ---
def test_scope_vocabulary_has_a_single_source():
    from pi_gw_panel.api.schemas import TokenCreateIn
    from pi_gw_panel.auth import tokens as token_mod
    accepted = TokenCreateIn.model_fields["scope"].annotation.__args__
    assert set(accepted) == set(token_mod.SCOPES)
    with pytest.raises(Exception):
        TokenCreateIn(name="x", scope="admin")
