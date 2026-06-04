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
from pi_gw_panel.health import failover
from pi_gw_panel.health.monitor import HealthMonitor
from pi_gw_panel.auth.auth import SESSION_AUTHED
from pi_gw_panel import logs as logs_mod


def _active_health(store) -> dict | None:
    """Active node's health snapshot for the live graph (reuses node_health)."""
    aid = store.get_setting("active_node_id")
    if not aid:
        return None
    h = store.get_health(int(aid))
    if h is None:
        return None
    return {"node_id": h.node_id, "real_ok": h.last_real_ok,
            "latency_ms": h.last_real_ms, "egress_ip": h.egress_ip}


def _traffic_frame(state) -> dict:
    """Blocking: sample throughput (gRPC under the hood) + read active-node health."""
    return {
        "ts": int(time.time() * 1000),
        "outbounds": state.sampler.sample(),
        "active": _active_health(state.store),
    }


def create_app(settings: Settings, state: AppState | None = None) -> FastAPI:
    app_state = state if state is not None else build_state(settings)
    scheduler = SubScheduler(app_state)
    # Health monitor probes each interval, then auto-failover runs on the result
    # (wall-clock now → cooldown survives restarts). Both gate on their own settings.
    monitor = HealthMonitor(app_state, after_tick=lambda: failover.run(app_state, time.time()))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Boot/restart persistence: re-establish the active node's tunnel before serving,
        # so a reboot or container restart self-heals with no manual Connect (Plan 9c).
        from pi_gw_panel.controller import reapply_active_node
        res = reapply_active_node(app_state)
        if res is not None and not res.ok:
            logging.getLogger("pi_gw_panel").warning("boot reapply failed: %s", res.error)
        scheduler.start()
        monitor.start()
        try:
            yield
        finally:
            await scheduler.stop()
            await monitor.stop()

    logs_mod.setup_app_logging(app_state.settings.app_log)   # app logs → data_dir/app.log

    app = FastAPI(title="v2pi", lifespan=lifespan)
    app.state.app_state = app_state
    app.state.scheduler = scheduler
    app.state.monitor = monitor
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)
    # Register future middleware AFTER this line: Starlette runs middleware in
    # reverse order, so later-registered (e.g. auth) runs first and sees the session.

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.websocket("/api/ws/traffic")
    async def ws_traffic(websocket: WebSocket) -> None:
        # session-auth (same cookie as the REST API); reject the handshake if unauthed
        if not websocket.session.get(SESSION_AUTHED):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        state = websocket.app.state.app_state
        store = state.store
        if (store.get_setting("stats_enabled") or "1") != "1":
            await websocket.send_json({"disabled": True})
            await websocket.close()
            return
        interval = int(store.get_setting("traffic_sample_ms") or "1000") / 1000.0
        loop = asyncio.get_running_loop()
        try:
            while True:
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
