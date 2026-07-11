import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pi_gw_panel.models import NodeHealth
from pi_gw_panel.health import probe

log = logging.getLogger("pi_gw_panel")
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

    # N5: a node that failed BOTH direct probes sits out a growing number of sweeps
    # (1 → 3 → 7, capped) so a dead pool doesn't burn a slow probe budget every tick;
    # one success resets it. The active node is never skipped.
    BACKOFF_MAX_SKIP = 7

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
        self._backoff: dict[int, dict] = {}   # node_id → {"streak": int, "skip": int}

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

        # N5: skip nodes that are sitting out a backoff window (decrement their counter);
        # their previous health rows stay as-is. The active node is always probed.
        due = []
        for node in nodes:
            bo = self._backoff.get(node.id)
            if node.id != active_id and bo is not None and bo["skip"] > 0:
                bo["skip"] -= 1
                continue
            due.append(node)
        self._backoff = {nid: bo for nid, bo in self._backoff.items()
                         if any(n.id == nid for n in nodes)}   # forget deleted nodes

        # Direct probes for every DUE node, concurrently: TCP liveness + an HTTPS handshake.
        # The handshake fills the "real" column for all nodes — a real request *through* each
        # node isn't possible (xray has a single active outbound).
        def direct(node):
            tcp_ok, tcp_ms = self._tcp_ping(node.address, node.port)
            http_ok, http_ms = self._http_ping(node.address, node.port, node.sni)
            return node, tcp_ok, tcp_ms, http_ok, http_ms

        if not due:
            return
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(due)))) as ex:
            swept = list(ex.map(direct, due))

        for node, tcp_ok, tcp_ms, http_ok, http_ms in swept:
            if tcp_ok or http_ok:
                # decay rather than hard-reset: a chronically-flapping node (one success every N
                # sweeps) still accrues some backoff instead of being probed every sweep forever.
                bo = self._backoff.get(node.id)
                if bo is not None:
                    bo["streak"] = max(0, bo["streak"] - 1)
                    if bo["streak"] == 0:
                        self._backoff.pop(node.id, None)
                    else:
                        bo["skip"] = min(self.BACKOFF_MAX_SKIP, 2 ** min(bo["streak"], 3) - 1)
            elif node.id != active_id:
                bo = self._backoff.setdefault(node.id, {"streak": 0, "skip": 0})
                bo["streak"] += 1
                bo["skip"] = min(self.BACKOFF_MAX_SKIP, 2 ** min(bo["streak"], 3) - 1)
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
                    egress_ip6=(prev.egress_ip6 if prev else None),   # fast loop owns v6 egress — don't wipe it
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
                    egress_ip6=(prev.egress_ip6 if prev else None),
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

    def _safe_tick(self) -> None:
        # one bad sweep (store error, malformed node row) must never kill the loop — log & continue,
        # else the whole health sweep stops forever and failover runs on frozen data.
        try:
            self._tick()
        except Exception:
            log.exception("health monitor sweep failed")

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        # run one sweep immediately so health engages right after start instead of serving the
        # stale pre-restart snapshot for a full interval (up to 30 min).
        await loop.run_in_executor(None, self._safe_tick)
        while True:
            await asyncio.sleep(self._interval())
            await loop.run_in_executor(None, self._safe_tick)
