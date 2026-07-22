import time
from fastapi import Request, HTTPException, Header
from pi_gw_panel.state import AppState
from pi_gw_panel.auth.auth import (
    SESSION_AUTHED, SESSION_CSRF, SESSION_EPOCH, SESSION_LASTSEEN, csrf_matches)
from pi_gw_panel.auth.tokens import hash_token


def get_state(request: Request) -> AppState:
    return request.app.state.app_state


def session_invalid_reason(session, store, *, touch: bool = True) -> str | None:
    """Shared session-validity check for REST (require_auth) and the traffic WebSocket.

    Returns None when the session is good, else a short reason. On a live, timeout-enabled
    session it refreshes last_seen as a side effect. Pure w.r.t. the transport (takes a
    plain session mapping + store), so the WS handshake enforces the SAME epoch / idle-timeout
    rules the REST API does (audit D2) instead of just the authed flag."""
    if not session.get(SESSION_AUTHED):
        return "auth required"
    # a password change bumps the stored epoch → older sessions (lower/absent epoch) are out
    if session.get(SESSION_EPOCH, 0) != int(store.get_setting("session_epoch") or "0"):
        return "session expired"
    # optional idle timeout (session_timeout_min, 0 = off)
    timeout_min = int(store.get_setting("session_timeout_min") or "0")
    if timeout_min > 0:
        now = int(time.time())
        if now - session.get(SESSION_LASTSEEN, now) > timeout_min * 60:
            return "session idle timeout"
        if touch:
            session[SESSION_LASTSEEN] = now
    return None


def _token_principal(request: Request) -> dict | None:
    """Resolve an `Authorization: Bearer <token>` to {scope, id, prefix}, or None. Cached
    per-request so require_auth + require_csrf (and the audit middleware) agree and the DB is
    hit at most once. Only fires when a Bearer header is present (cookie/session requests
    carry none → no token lookup)."""
    if hasattr(request.state, "_token_principal"):
        return request.state._token_principal
    p = None
    header = request.headers.get("authorization", "")
    if header[:7].lower() == "bearer ":
        secret = header[7:].strip()
        if secret:
            store = request.app.state.app_state.store
            row = store.get_token_by_hash(hash_token(secret))
            if row is not None:
                expires_at = row.get("expires_at")
                if expires_at is None or int(expires_at) > int(time.time()):
                    store.touch_token(int(row["id"]))
                    p = {"scope": row["scope"], "id": int(row["id"]),
                         "prefix": row.get("prefix", ""), "expires_at": expires_at}
    request.state._token_principal = p
    return p


def require_auth(request: Request) -> None:
    # A valid API token (read or readwrite) authenticates — this gate covers reads.
    token = _token_principal(request)
    if token is not None:
        if token["scope"] == "monitor":
            allowed = request.method == "GET" and request.url.path in {
                "/api/status", "/api/traffic/history", "/api/node-health", "/api/network",
            }
            if not allowed:
                raise HTTPException(status_code=403, detail="monitor token cannot access secrets")
        return
    store = request.app.state.app_state.store
    reason = session_invalid_reason(request.session, store)
    if reason is not None:
        if reason == "session idle timeout":
            request.session.clear()
        raise HTTPException(status_code=401, detail=reason)


def require_csrf(request: Request, x_csrf_token: str | None = Header(default=None)) -> None:
    # Mutating routes carry this gate. Token auth: require the readwrite scope (no CSRF — token
    # auth isn't cookie-based, so it isn't CSRF-exposed). Session auth: double-submit CSRF as before.
    token = _token_principal(request)
    if token is not None:
        if token["scope"] != "readwrite":
            raise HTTPException(status_code=403, detail="token is read-only")
        return
    if not csrf_matches(request.session.get(SESSION_CSRF), x_csrf_token):
        raise HTTPException(status_code=403, detail="bad csrf token")
