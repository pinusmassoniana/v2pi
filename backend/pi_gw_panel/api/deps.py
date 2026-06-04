import time
from fastapi import Request, HTTPException, Header
from pi_gw_panel.state import AppState
from pi_gw_panel.auth.auth import (
    SESSION_AUTHED, SESSION_CSRF, SESSION_EPOCH, SESSION_LASTSEEN, csrf_matches)


def get_state(request: Request) -> AppState:
    return request.app.state.app_state


def require_auth(request: Request) -> None:
    if not request.session.get(SESSION_AUTHED):
        raise HTTPException(status_code=401, detail="auth required")
    store = request.app.state.app_state.store
    # a password change bumps the stored epoch → older sessions (lower/absent epoch) are out
    if request.session.get(SESSION_EPOCH, 0) != int(store.get_setting("session_epoch") or "0"):
        raise HTTPException(status_code=401, detail="session expired")
    # optional idle timeout (session_timeout_min, 0 = off)
    timeout_min = int(store.get_setting("session_timeout_min") or "0")
    if timeout_min > 0:
        now = int(time.time())
        if now - request.session.get(SESSION_LASTSEEN, now) > timeout_min * 60:
            request.session.clear()
            raise HTTPException(status_code=401, detail="session idle timeout")
        request.session[SESSION_LASTSEEN] = now


def require_csrf(request: Request, x_csrf_token: str | None = Header(default=None)) -> None:
    if not csrf_matches(request.session.get(SESSION_CSRF), x_csrf_token):
        raise HTTPException(status_code=403, detail="bad csrf token")
