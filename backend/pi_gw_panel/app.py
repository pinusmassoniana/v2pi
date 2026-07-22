import asyncio
import inspect
import logging
import os
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from starlette.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from pi_gw_panel.config import Settings
from pi_gw_panel.state import AppState, build_state
from pi_gw_panel.subs.scheduler import SubScheduler
from pi_gw_panel.health.monitor import HealthMonitor
from pi_gw_panel.health.liveness import LivenessLoop
from pi_gw_panel import logs as logs_mod
from pi_gw_panel.health.snapshot import active_health
from pi_gw_panel.api.deps import _token_principal, session_invalid_reason
from pi_gw_panel.auth.auth import SESSION_AUTHED
from pi_gw_panel.stats.history import bounded_interval_ms
from pi_gw_panel.api.schemas import ReadinessOut


AUTH_BODY_LIMIT = 4 * 1024
IMPORT_BODY_LIMIT = 600 * 1024
RESTORE_BODY_LIMIT = 2 * 1024 * 1024
DEFAULT_BODY_LIMIT = 1024 * 1024


class RequestBodyLimitMiddleware:
    """Reject declared and streamed oversized bodies before JSON/Pydantic buffering."""

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _limit(path: str) -> int:
        if path in ("/api/login", "/api/setup"):
            return AUTH_BODY_LIMIT
        if path == "/api/nodes/import":
            return IMPORT_BODY_LIMIT
        if path == "/api/restore":
            return RESTORE_BODY_LIMIT
        return DEFAULT_BODY_LIMIT

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit(scope.get("path", ""))
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = 0
        if declared > limit:
            await JSONResponse({"detail": "request body too large"}, status_code=413)(
                scope, receive, send)
            return

        consumed = 0
        buffered = []
        while True:
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > limit:
                    await JSONResponse(
                        {"detail": "request body too large"}, status_code=413)(
                            scope, receive, send)
                    return
            buffered.append(message)
            if message["type"] == "http.disconnect" or not message.get("more_body", False):
                break

        async def replay_receive():
            if buffered:
                return buffered.pop(0)
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)


def _traffic_frame(state) -> dict:
    """Enrich the recorder's immutable latest sample with health and durable totals.

    `totals` is the recorder sampler's process-lifetime counters (reset on xray restart);
    `lifetime` is the recorder-accumulated total that survives restarts (audit F)."""
    recorder = getattr(state, "recorder", None)
    frame = recorder.snapshot() if recorder is not None else {
        "error": "stats recorder unavailable", "stale": True}
    totals = (frame.get("totals") or {}).get("proxy") or {"up": 0, "down": 0}
    store = state.store
    lifetime = {"up": int(store.get_setting("data_used_up") or "0"),
                "down": int(store.get_setting("data_used_down") or "0")}
    # NF4: data used "this session" = lifetime − the baseline snapshotted at the last (re)connect.
    base_up = int(store.get_setting("session_base_up") or "0")
    base_down = int(store.get_setting("session_base_down") or "0")
    session = {"up": max(0, lifetime["up"] - base_up), "down": max(0, lifetime["down"] - base_down)}
    stats_client = getattr(state, "stats_client", None)
    frame.update({
        "ts": frame.get("ts", int(time.time() * 1000)),
        "outbounds": frame.get("outbounds", {}),
        "totals": totals,
        "active": active_health(store),
        "lifetime": lifetime,
        "session": session,
        "stats": stats_client.status() if stats_client is not None else {
            "last_ok_at": None, "last_error": "stats client unavailable", "fail_count": 0},
    })
    return frame


def create_app(settings: Settings, state: AppState | None = None) -> FastAPI:
    if not settings.loopback_bind and not settings.tls_enabled:
        raise ValueError("non-loopback management bind requires TLS certificate and key")
    app_state = state if state is not None else build_state(settings)
    scheduler = SubScheduler(app_state)
    # Slow (30-min) all-node TCP/HTTPS sweep for the health table + latency trends.
    monitor = HealthMonitor(app_state)
    # Fast (30-s) resilience loop: xray crash-watchdog + active-node real-probe + auto-failover
    # (wall-clock now → cooldown survives restarts). This is the failover driver now (B1/B2).
    liveness = LivenessLoop(app_state)
    from pi_gw_panel.backup.scheduler import BackupScheduler
    backup_scheduler = BackupScheduler(app_state)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Boot/restart persistence: re-establish the active node's tunnel before serving,
        # so a reboot or container restart self-heals with no manual Connect (Plan 9c).
        # First close the boot leak window: with the kill-switch on, install the leak-guard
        # BEFORE the tunnel comes up so a reboot never leaks client→WAN (A1).
        owned = []
        log_handler = None
        try:
            log_handler = logs_mod.setup_app_logging(app_state.settings.app_log)
            from pi_gw_panel.controller import reapply_active_node, boot_guard
            from pi_gw_panel.net_control.provision import host_provision
            # Provisioning can partially start these resources, so register their cleanup first.
            for resource in (getattr(app_state, "dnsmasq", None),
                             getattr(app_state, "pd_client", None)):
                if resource is not None:
                    owned.append(resource)
            host_provision(app_state)
            boot_guard(app_state)
            res = reapply_active_node(app_state)
            if res is not None and not res.ok:
                logging.getLogger("pi_gw_panel").warning("boot reapply failed: %s", res.error)
            for component in (scheduler, monitor, liveness, backup_scheduler,
                              app_state.recorder):
                if component is None:
                    continue
                owned.append(component)
                component.start()
            yield
        finally:
            for component in reversed(owned):
                try:
                    result = component.stop()
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    logging.getLogger("pi_gw_panel").warning(
                        "component shutdown failed: %s", type(component).__name__, exc_info=True)
            app_state.close()
            logs_mod.teardown_app_logging(log_handler)

    app = FastAPI(title="v2pi", lifespan=lifespan)
    app.state.app_state = app_state
    app.state.scheduler = scheduler
    app.state.monitor = monitor
    app.state.login_guard = {}   # per-client-IP login rate-limit buckets (SS3 / audit B8)
    # Added before audit/session below so the final ASGI order is Session → Audit → BodyLimit.
    # It therefore rejects before body parsing while the audit still records a 413 mutation.
    app.add_middleware(RequestBodyLimitMiddleware)
    # B7: never run on the dataclass dev default. The CLI entrypoint replaces it with a
    # persisted secret; any other entry path (uvicorn factory, direct import) gets a fresh
    # ephemeral one — sessions won't survive a restart there, but they're never forgeable.
    secret = settings.session_secret
    if secret and secret != "dev-insecure-secret" and len(secret.encode("utf-8")) < 32:
        raise ValueError("session_secret must be at least 32 bytes")
    if not secret or secret == "dev-insecure-secret":
        import secrets as _secrets
        secret = _secrets.token_urlsafe(32)
        logging.getLogger("pi_gw_panel").warning(
            "session_secret unset/dev-default — using an ephemeral secret (sessions reset on restart)")
    # SameSite=strict (first-party SPA) + a bounded lifetime, defense-in-depth atop CSRF.
    @app.middleware("http")
    async def audit_mw(request, call_next):
        """Record the pre-endpoint principal and the final result of every API mutation."""
        mutating = (request.method in ("POST", "PUT", "PATCH", "DELETE")
                    and request.url.path.startswith("/api"))
        actor = "anon"
        if mutating:
            try:
                principal = _token_principal(request)
                if principal is not None:
                    actor = f"token:{principal.get('prefix', principal['id'])}"
                elif request.session.get(SESSION_AUTHED):
                    actor = f"user:{app_state.store.get_setting('auth_username') or '?'}"
            except Exception:
                logging.getLogger("pi_gw_panel").debug(
                    "audit principal resolution failed", exc_info=True)
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            if mutating:
                try:
                    app_state.store.add_audit(
                        int(time.time()), actor, request.method, request.url.path, status)
                except Exception:
                    logging.getLogger("pi_gw_panel").debug(
                        "audit log write failed", exc_info=True)

    # Session must wrap the audit middleware so it can resolve the principal before logout.
    app.add_middleware(SessionMiddleware, secret_key=secret,
                       same_site="strict", max_age=7 * 24 * 3600,
                       https_only=settings.tls_enabled)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/ready", response_model=ReadinessOut,
             responses={503: {"model": ReadinessOut}})
    def ready():
        from pi_gw_panel.net_control import netcheck
        checks = netcheck.readiness_checks(app_state)
        is_ready = all(checks.values())
        payload = {"status": "ready" if is_ready else "not_ready", "checks": checks}
        return JSONResponse(payload, status_code=200 if is_ready else 503)

    @app.websocket("/api/ws/traffic")
    async def ws_traffic(websocket: WebSocket) -> None:
        state = websocket.app.state.app_state
        store = state.store
        # session-auth (same cookie as the REST API) — enforce the FULL contract (epoch +
        # idle timeout), not just the authed flag, so a password change / idle-out kills the
        # traffic stream too (audit D2).
        if session_invalid_reason(websocket.session, store) is not None:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        disabled_sent = False
        try:
            while True:
                # Revalidate established sockets too: password epoch rotation and idle expiry
                # must revoke a stream that was valid at handshake time.
                if session_invalid_reason(websocket.session, store, touch=False) is not None:
                    await websocket.close(code=4401)
                    return
                enabled = (store.get_setting("stats_enabled") or "1") == "1"
                if not enabled:
                    if not disabled_sent:
                        await websocket.send_json({"disabled": True})
                        disabled_sent = True
                    await asyncio.sleep(5.0)  # idle cheaply; a still-open client can re-enable
                    continue
                disabled_sent = False
                interval = bounded_interval_ms(
                    store.get_setting("traffic_sample_ms") or "1000") / 1000.0
                frame = _traffic_frame(state)  # no I/O: detached recorder snapshot only
                await websocket.send_json(frame)
                await asyncio.sleep(interval)
        except (WebSocketDisconnect, RuntimeError):
            return  # client gone / socket closed — end the loop

    from pi_gw_panel.api.routes import router
    app.include_router(router)

    if settings.static_dir and os.path.isdir(settings.static_dir):
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="spa")

    return app
