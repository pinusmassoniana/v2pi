import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from pi_gw_panel.config import Settings
from pi_gw_panel.state import AppState, build_state
from pi_gw_panel.subs.scheduler import SubScheduler
from pi_gw_panel.health.monitor import HealthMonitor
from pi_gw_panel.health.liveness import LivenessLoop
from pi_gw_panel import logs as logs_mod
from pi_gw_panel.health.snapshot import active_health
from pi_gw_panel.api.deps import session_invalid_reason


def _traffic_frame(state) -> dict:
    """Blocking: sample throughput (gRPC under the hood) + active-node health snapshot
    (shared with the Network status) + cumulative data-used totals for the proxy outbound.

    `totals` is the live sampler's process-lifetime counters (reset on xray restart);
    `lifetime` is the recorder-accumulated total that survives restarts (audit F)."""
    outbounds = state.sampler.sample()
    totals = (getattr(state.sampler, "totals", {}) or {}).get("proxy") or {"up": 0, "down": 0}
    store = state.store
    lifetime = {"up": int(store.get_setting("data_used_up") or "0"),
                "down": int(store.get_setting("data_used_down") or "0")}
    return {
        "ts": int(time.time() * 1000),
        "outbounds": outbounds,
        "active": active_health(store),
        "totals": totals,
        "lifetime": lifetime,
    }


def create_app(settings: Settings, state: AppState | None = None) -> FastAPI:
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
        from pi_gw_panel.controller import reapply_active_node, boot_guard
        boot_guard(app_state)
        res = reapply_active_node(app_state)
        if res is not None and not res.ok:
            logging.getLogger("pi_gw_panel").warning("boot reapply failed: %s", res.error)
        scheduler.start()
        monitor.start()
        liveness.start()
        backup_scheduler.start()
        if app_state.recorder is not None:
            app_state.recorder.start()          # always-on traffic history sampler
        try:
            yield
        finally:
            await scheduler.stop()
            await monitor.stop()
            await liveness.stop()
            await backup_scheduler.stop()
            if app_state.recorder is not None:
                await app_state.recorder.stop()

    logs_mod.setup_app_logging(app_state.settings.app_log)   # app logs → data_dir/app.log

    app = FastAPI(title="v2pi", lifespan=lifespan)
    app.state.app_state = app_state
    app.state.scheduler = scheduler
    app.state.monitor = monitor
    app.state.login_guard = {"count": 0, "until": 0.0}   # per-app login rate-limit (SS3)
    # SameSite=strict (first-party SPA) + a bounded lifetime, defense-in-depth atop CSRF.
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret,
                       same_site="strict", max_age=7 * 24 * 3600)
    # Register future middleware AFTER this line: Starlette runs middleware in
    # reverse order, so later-registered (e.g. auth) runs first and sees the session.

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

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
        if (store.get_setting("stats_enabled") or "1") != "1":
            await websocket.send_json({"disabled": True})
            await websocket.close()
            return
        loop = asyncio.get_running_loop()
        try:
            while True:
                # re-read the cadence each tick so a Settings change takes effect without a
                # reconnect, floored to match the recorder (audit D3).
                interval = max(0.5, int(store.get_setting("traffic_sample_ms") or "1000") / 1000.0)
                # offload the blocking gRPC sample to a thread so the loop stays free
                try:
                    frame = await loop.run_in_executor(None, _traffic_frame, state)
                except Exception as exc:                       # stats/xray down → gap, don't drop the socket
                    await websocket.send_json({"error": str(exc)})
                    await asyncio.sleep(interval)
                    continue
                await websocket.send_json(frame)
                await asyncio.sleep(interval)
        except (WebSocketDisconnect, RuntimeError):
            return  # client gone / socket closed — end the loop

    from pi_gw_panel.api.routes import router
    app.include_router(router)

    if settings.static_dir and os.path.isdir(settings.static_dir):
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="spa")

    return app
