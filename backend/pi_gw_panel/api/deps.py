from fastapi import Request, HTTPException, Header
from pi_gw_panel.state import AppState
from pi_gw_panel.auth.auth import SESSION_AUTHED, SESSION_CSRF, csrf_matches


def get_state(request: Request) -> AppState:
    return request.app.state.app_state


def require_auth(request: Request) -> None:
    if not request.session.get(SESSION_AUTHED):
        raise HTTPException(status_code=401, detail="auth required")


def require_csrf(request: Request, x_csrf_token: str | None = Header(default=None)) -> None:
    if not csrf_matches(request.session.get(SESSION_CSRF), x_csrf_token):
        raise HTTPException(status_code=403, detail="bad csrf token")
