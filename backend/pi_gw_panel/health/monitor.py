import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pi_gw_panel.models import NodeHealth
from pi_gw_panel.health import probe

DEFAULT_PROBE_URL = "https://api.ipify.org?format=json"
DEFAULT_INTERVAL = 1800.0   # 30 min — TCP + direct-HTTPS sweep of all nodes (configurable)


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
                 http_ping=probe.http_ping, now_iso=None, tick_sec: float | None = None,
                 after_tick=None):
        self._state = state
        self._tcp_ping = tcp_ping
        self._real_request = real_request
        self._http_ping = http_ping
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
        # The through-tunnel real probe needs the local proxy inbound, which only exists when
        # tunneled_fetch is on. With it off the port isn't listening, so probing it would fail
        # every tick and spuriously fail the active node over (NC1) — skip the real probe then.
        tunneled_on = (store.get_setting("tunneled_fetch") or "1") == "1"
        ts = self._now_iso()
        nodes = store.list_nodes()

        # Direct probes for EVERY node, concurrently: TCP liveness + an HTTPS handshake.
        # The handshake fills the "real" column for all nodes — a real request *through* each
        # node isn't possible (xray has a single active outbound).
        def direct(node):
            tcp_ok, tcp_ms = self._tcp_ping(node.address, node.port)
            http_ok, http_ms = self._http_ping(node.address, node.port, node.sni)
            return node, tcp_ok, tcp_ms, http_ok, http_ms

        with ThreadPoolExecutor(max_workers=24) as ex:
            swept = list(ex.map(direct, nodes))

        for node, tcp_ok, tcp_ms, http_ok, http_ms in swept:
            prev = store.get_health(node.id)
            if node.id == active_id and tunneled_on:
                # active node: the through-tunnel real request gives the true egress IP and
                # drives the failover hysteresis counter — distinct from the direct HTTP probe.
                real_ok, _status, real_ms, egress = self._real_request(proxy_url, probe_url)
                prev_fail = prev.fail_count if prev else 0
                store.upsert_health(NodeHealth(
                    node_id=node.id, last_tcp_ok=tcp_ok, last_tcp_ms=tcp_ms,
                    last_http_ok=http_ok, last_http_ms=http_ms,
                    last_real_ok=real_ok, last_real_ms=real_ms, egress_ip=egress,
                    checked_at=ts, fail_count=0 if real_ok else prev_fail + 1))
            else:
                # non-active (or active with no proxy port): direct probes only. Preserve any
                # prior real/egress (NC2 — don't wipe a per-node "T" result) and keep
                # fail_count at 0 since there's no real-request failure signal (NC1).
                store.upsert_health(NodeHealth(
                    node_id=node.id, last_tcp_ok=tcp_ok, last_tcp_ms=tcp_ms,
                    last_http_ok=http_ok, last_http_ms=http_ms,
                    last_real_ok=(prev.last_real_ok if prev else None),
                    last_real_ms=(prev.last_real_ms if prev else None),
                    egress_ip=(prev.egress_ip if prev else None),
                    checked_at=ts, fail_count=0))
            if http_ok and http_ms is not None:
                store.record_latency(node.id, http_ms)   # NN4: HTTPS-handshake latency trend

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
