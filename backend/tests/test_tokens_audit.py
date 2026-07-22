import time

from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.auth import tokens


# --- token primitives ---
def test_generate_shape_and_uniqueness():
    full, h, prefix = tokens.generate()
    assert full.startswith("pgwp_")
    assert h == tokens.hash_token(full) and len(h) == 64      # sha256 hex
    assert prefix == full[:12]
    assert tokens.generate()[0] != tokens.generate()[0]       # random each time


# --- store CRUD ---
def _store():
    conn = connect(":memory:")
    init_schema(conn)                  # runs migration 11 → api_tokens table
    return NodeStore(conn)


def test_store_token_crud():
    s = _store()
    full, h, prefix = tokens.generate()
    row = s.create_token("ci", "readwrite", h, prefix)
    assert row["id"] > 0 and row["scope"] == "readwrite" and row["last_used_at"] is None
    assert [t["name"] for t in s.list_tokens()] == ["ci"]
    got = s.get_token_by_hash(h)
    assert got["scope"] == "readwrite" and got["id"] == row["id"]
    assert s.get_token_by_hash(tokens.hash_token("nope")) is None
    s.touch_token(row["id"])
    assert s.list_tokens()[0]["last_used_at"] is not None
    assert s.delete_token(row["id"]) is True
    assert s.delete_token(row["id"]) is False                 # already gone → False
    assert s.list_tokens() == []


def test_store_token_expiry_metadata():
    s = _store()
    _, token_hash, prefix = tokens.generate()
    row = s.create_token("temporary", "monitor", token_hash, prefix, expires_at=12345)
    assert row["expires_at"] == 12345
    assert s.get_token_by_hash(token_hash)["expires_at"] == 12345


def test_store_never_persists_the_plaintext_secret():
    s = _store()
    full, h, prefix = tokens.generate()
    s.create_token("ci", "read", h, prefix)
    blob = " ".join(str(dict(r)) for r in
                    s._conn.execute("SELECT * FROM api_tokens").fetchall())
    assert full not in blob and h in blob                     # only the hash is stored


# --- API: auth + scope enforcement ---
def _app(settings, stub_xray):
    settings.xray_bin = stub_xray
    return create_app(settings, state=build_state(settings, net=DryRunBackend()))


def _login(c):
    c.post("/api/setup", json={"username": "admin", "password": "changeme"})
    return c.get("/api/csrf").json()["csrf"]


def test_token_lifecycle_and_scopes(settings, stub_xray):
    app = _app(settings, stub_xray)
    admin = TestClient(app)                       # session-authed client
    csrf = _login(admin)

    rw = admin.post("/api/tokens", json={"name": "ci", "scope": "readwrite"},
                    headers={"X-CSRF-Token": csrf})
    assert rw.status_code == 201
    rw_json = rw.json()
    rw_secret, rw_id = rw_json["token"], rw_json["id"]
    assert rw_secret.startswith("pgwp_")
    ro_secret = admin.post("/api/tokens", json={"name": "monitor", "scope": "read"},
                           headers={"X-CSRF-Token": csrf}).json()["token"]

    # list returns metadata only — never the secret
    lst = admin.get("/api/tokens").json()
    assert {t["name"] for t in lst} == {"ci", "monitor"}
    assert all("token" not in t for t in lst)
    assert all(t["prefix"].startswith("pgwp_") for t in lst)

    # token-only client (no session cookie)
    bearer = TestClient(app)
    rw_h = {"Authorization": f"Bearer {rw_secret}"}
    ro_h = {"Authorization": f"Bearer {ro_secret}"}
    node = {"name": "n", "address": "1.2.3.4", "port": 47000, "uuid": "u",
            "sni": "www.microsoft.com", "public_key": "PK", "short_id": "ab12"}

    # read token: GET ok, any write → 403 (no CSRF needed; scope is the gate)
    assert bearer.get("/api/status", headers=ro_h).status_code == 200
    assert bearer.post("/api/nodes", json=node, headers=ro_h).status_code == 403

    # readwrite token: GET + mutation ok WITHOUT a CSRF header (token auth bypasses CSRF)
    assert bearer.get("/api/status", headers=rw_h).status_code == 200
    assert bearer.post("/api/nodes", json=node, headers=rw_h).status_code == 200

    # invalid token → 401
    assert bearer.get("/api/status",
                      headers={"Authorization": "Bearer pgwp_bogus"}).status_code == 401

    # revoke the readwrite token → it stops authenticating; unknown id → 404
    assert admin.delete(f"/api/tokens/{rw_id}", headers={"X-CSRF-Token": csrf}).status_code == 204
    assert bearer.get("/api/status", headers=rw_h).status_code == 401
    assert admin.delete("/api/tokens/999999", headers={"X-CSRF-Token": csrf}).status_code == 404

    # bad scope / empty name → 422
    assert admin.post("/api/tokens", json={"name": "x", "scope": "admin"},
                      headers={"X-CSRF-Token": csrf}).status_code == 422
    assert admin.post("/api/tokens", json={"name": "", "scope": "read"},
                      headers={"X-CSRF-Token": csrf}).status_code == 422


def test_monitor_scope_is_redacted_and_expired_tokens_are_rejected(settings, stub_xray):
    app = _app(settings, stub_xray)
    admin = TestClient(app)
    csrf = _login(admin)
    created = admin.post(
        "/api/tokens",
        json={"name": "grafana", "scope": "monitor", "expires_at": int(time.time()) + 3600},
        headers={"X-CSRF-Token": csrf},
    ).json()
    monitor = {"Authorization": f"Bearer {created['token']}"}
    bearer = TestClient(app)
    assert bearer.get("/api/status", headers=monitor).status_code == 200
    assert bearer.get("/api/traffic/history", headers=monitor).status_code == 200
    assert bearer.get("/api/node-health", headers=monitor).status_code == 200
    assert bearer.get("/api/network", headers=monitor).status_code == 200
    for path in ("/api/backup", "/api/logs", "/api/nodes", "/api/subs", "/api/tokens", "/api/audit"):
        assert bearer.get(path, headers=monitor).status_code == 403, path

    expired, expired_hash, expired_prefix = tokens.generate()
    app.state.app_state.store.create_token(
        "expired", "monitor", expired_hash, expired_prefix, expires_at=int(time.time()) - 1)
    assert bearer.get(
        "/api/status", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
    expired_row = next(t for t in app.state.app_state.store.list_tokens() if t["name"] == "expired")
    assert expired_row["last_used_at"] is None


def test_legacy_read_scope_remains_secret_bearing_admin_read(settings, stub_xray):
    app = _app(settings, stub_xray)
    admin = TestClient(app)
    csrf = _login(admin)
    secret = admin.post(
        "/api/tokens", json={"name": "legacy", "scope": "read"},
        headers={"X-CSRF-Token": csrf}).json()["token"]
    bearer = TestClient(app)
    assert bearer.get(
        "/api/backup", headers={"Authorization": f"Bearer {secret}"}).status_code == 200
