import asyncio
from datetime import datetime, timezone
from pi_gw_panel.models import NodeHealth
from pi_gw_panel.health import probe

DEFAULT_PROBE_URL = "https://api.ipify.org?format=json"
DEFAULT_INTERVAL = 30.0


class HealthMonitor:
    """Background asyncio loop (mirrors subs/scheduler.py): every `health_interval`
    seconds it TCP-pings every node and runs a real HTTPS request through the active
    node's local http proxy, persisting the result to `node_health`. Blocking probes
    are offloaded to a thread; the loop is cancellable.

    The active node's `fail_count` accumulates consecutive real-request failures (reset
    on success) — this is the hysteresis counter the failover logic reads. Probes are
    injected (`tcp_ping`/`real_request`) so tests run with no network. `after_tick` (if
    given) runs right after each probe sweep — the lifespan wires failover here."""

    def __init__(self, state, tcp_ping=probe.tcp_ping, real_request=probe.real_request,
                 now_iso=None, tick_sec: float | None = None, after_tick=None):
        self._state = state
        self._tcp_ping = tcp_ping
        self._real_request = real_request
        self._now_iso = now_iso or (lambda: datetime.now(timezone.utc).isoformat())
        self._tick_override = tick_sec
        self._after_tick = after_tick
        self._task: asyncio.Task | None = None

    # --- config knobs (settings k/v, with defaults) ---
    def _enabled(self) -> bool:
        return (self._state.store.get_setting("health_enabled") or "1") == "1"

    def _interval(self) -> float:
        if self._tick_override is not None:
            return self._tick_override
        v = self._state.store.get_setting("health_interval")
        return float(v) if v else DEFAULT_INTERVAL

    def _active_id(self) -> int | None:
        v = self._state.store.get_setting("active_node_id")
        return int(v) if v else None

    # --- one probe sweep ---
    def run_once(self) -> None:
        if not self._enabled():
            return
        store = self._state.store
        active_id = self._active_id()
        probe_url = store.get_setting("health_probe_url") or DEFAULT_PROBE_URL
        proxy_url = f"http://127.0.0.1:{self._state.settings.local_proxy_port}"
        ts = self._now_iso()
        for node in store.list_nodes():
            tcp_ok, tcp_ms = self._tcp_ping(node.address, node.port)
            if node.id == active_id:
                real_ok, _status, real_ms, egress = self._real_request(proxy_url, probe_url)
                prev = store.get_health(node.id)
                prev_fail = prev.fail_count if prev else 0
                store.upsert_health(NodeHealth(
                    node_id=node.id, last_tcp_ok=tcp_ok, last_tcp_ms=tcp_ms,
                    last_real_ok=real_ok, last_real_ms=real_ms, egress_ip=egress,
                    checked_at=ts, fail_count=0 if real_ok else prev_fail + 1))
            else:
                # non-active: TCP liveness only (real fields cleared, counter reset)
                store.upsert_health(NodeHealth(
                    node_id=node.id, last_tcp_ok=tcp_ok, last_tcp_ms=tcp_ms,
                    checked_at=ts, fail_count=0))

    def _tick(self) -> None:
        self.run_once()
        if self._after_tick is not None:
            self._after_tick()

    # --- loop lifecycle ---
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval())
            await asyncio.get_running_loop().run_in_executor(None, self._tick)
