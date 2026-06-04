import time
from fastapi import Request, HTTPException, Header
from pi_gw_panel.state import AppState
from pi_gw_panel.auth.auth import (
    SESSION_AUTHED, SESSION_CSRF, SESSION_EPOCH, SESSION_LASTSEEN, csrf_matches)


def get_state(request: Request) -> AppState:
    return request.app.state.app_state


def session_invalid_reason(session, store) -> str | None:
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
        session[SESSION_LASTSEEN] = now
    return None


def require_auth(request: Request) -> None:
    store = request.app.state.app_state.store
    reason = session_invalid_reason(request.session, store)
    if reason is not None:
        if reason == "session idle timeout":
            request.session.clear()
        raise HTTPException(status_code=401, detail=reason)


def require_csrf(request: Request, x_csrf_token: str | None = Header(default=None)) -> None:
    if not csrf_matches(request.session.get(SESSION_CSRF), x_csrf_token):
        raise HTTPException(status_code=403, detail="bad csrf token")
