import asyncio
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pi_gw_panel.health import probe
from pi_gw_panel.controller import apply_lock
from pi_gw_panel.net_control import events

log = logging.getLogger("pi_gw_panel")
DEFAULT_PROBE_URL = "https://api.ipify.org?format=json"
DEFAULT_INTERVAL = 1800.0   # 30 min — TCP + direct-HTTPS sweep of all nodes (configurable)
MIN_INTERVAL = 5.0          # a hand-edited/restored 0 must not turn the loop into a spin
LOOP_ERROR_EVENT_GAP = 300.0


class LoopErrorReporter:
    """A loop that dies silently is the worst failure mode this subsystem has, so loop-level
    errors are recorded as panel-visible events — but the event ring holds only 40 entries, so
    a persistent error must not be allowed to flush every connect/failover record out of it.
    One entry per `gap` seconds is enough to see it."""

    def __init__(self, kind: str = "health-error", gap: float = LOOP_ERROR_EVENT_GAP):
        self._kind, self._gap, self._last = kind, gap, None

    def record(self, store, detail: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if self._last is not None and (now - self._last) < self._gap:
            return
        self._last = now
        events.record(store, self._kind, detail, now=now)


class HealthMonitor:
    """Background asyncio loop (mirrors subs/scheduler.py): every `health_interval`
    seconds it TCP-pings every node and runs a real HTTPS request through the active
    node's local http proxy, persisting the result to `node_health`.

    Blocking probes are offloaded to a thread. Cancelling the loop stops further *scheduling*
    immediately, but a probe already in flight is not interruptible and can outlive `stop()`
    by its own socket timeout; what is guaranteed is that once `stop()` has been called no
    probe result reaches the store (every write re-checks the stop event under `_write_lock`,
    which `stop()` holds while setting it) and that queued probes return without dialling.

    The active node's `fail_count` accumulates consecutive real-request failures (reset
    on success) — this is the hysteresis counter the failover logic reads. Probes are
    injected (`tcp_ping`/`real_request`) so tests run with no network. `after_tick` (if
    given) runs right after each probe sweep — the lifespan wires failover here."""

    # N5: a node that failed BOTH direct probes sits out a growing number of sweeps
    # (1 → 3 → 7, capped) so a dead pool doesn't burn a slow probe budget every tick;
    # one success resets it. The active node is never skipped.
    BACKOFF_MAX_SKIP = 7
    SHUTDOWN_TIMEOUT = 2.0

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
        self._executor: ThreadPoolExecutor | None = None
        self._future: Future | None = None
        self._stop_event = threading.Event()
        self._write_lock = threading.Lock()
        self._backoff: dict[int, dict] = {}   # node_id → {"streak": int, "skip": int}
        self._errors = LoopErrorReporter()

    # --- config knobs (settings k/v, with defaults) ---
    def _enabled(self) -> bool:
        """Master switch AND the sweep's own switch. The sweep is the expensive half — it probes
        every node in the pool — so it can be turned off on its own while the active node keeps
        being checked (and failover keeps working, since that reads the active check)."""
        store = self._state.store
        return ((store.get_setting("health_enabled") or "1") == "1"
                and (store.get_setting("health_sweep_enabled") or "1") == "1")

    def _interval(self) -> float:
        """Sweep cadence. Fully defensive: this is read OUTSIDE the tick guard, so a store
        blip or a restored non-numeric `health_interval` used to kill the loop task outright
        and take failover-to-standby down with it, permanently and silently."""
        if self._tick_override is not None:
            return self._tick_override
        try:
            v = self._state.store.get_setting("health_interval")
        except Exception:
            log.warning("health monitor: could not read health_interval", exc_info=True)
            return DEFAULT_INTERVAL
        try:
            return max(MIN_INTERVAL, float(v)) if v else DEFAULT_INTERVAL
        except (TypeError, ValueError):
            log.warning("health monitor: ignoring malformed health_interval %r", v)
            return DEFAULT_INTERVAL

    def _active_id(self) -> int | None:
        v = self._state.store.get_setting("active_node_id")
        return int(v) if v else None

    # --- one probe sweep ---
    def run_once(self, stop_event: threading.Event | None = None) -> None:
        if (stop_event is not None and stop_event.is_set()) or not self._enabled():
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
            # A queued probe must not start dialling after stop() — otherwise draining the pool
            # costs one full probe timeout per remaining node while shutdown waits.
            if stop_event is not None and stop_event.is_set():
                return None
            tcp_ok, tcp_ms = self._tcp_ping(node.address, node.port)
            http_ok, http_ms = self._http_ping(node.address, node.port, node.sni)
            return node, tcp_ok, tcp_ms, http_ok, http_ms

        if not due:
            return
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(due)))) as ex:
            swept = [result for result in ex.map(direct, due) if result is not None]

        if stop_event is not None and stop_event.is_set():
            return

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
            with apply_lock:
                with self._write_lock:
                    if stop_event is not None and stop_event.is_set():
                        return
                    direct_checked_at = None if self._active_id() == node.id else ts
                    store.update_health_direct(
                        node.id, tcp_ok=tcp_ok, tcp_ms=tcp_ms,
                        http_ok=http_ok, http_ms=http_ms, checked_at=direct_checked_at,
                    )
            if node.id == active_id and tunneled_on:
                # active node: the through-tunnel real request gives the true egress IP and
                # drives the failover hysteresis counter — distinct from the direct HTTP probe.
                with apply_lock:
                    still_active = self._active_id() == node.id
                if still_active:
                    real_ok, _status, real_ms, egress = self._real_request(proxy_url, probe_url)
                    with apply_lock:
                        with self._write_lock:
                            current_active = self._active_id()
                            if stop_event is not None and stop_event.is_set():
                                return
                            if current_active == node.id:
                                prev = store.get_health(node.id)
                                store.update_health_real(
                                    node.id, real_ok=real_ok, real_ms=real_ms, egress_ip=egress,
                                    egress_ip6=prev.egress_ip6 if prev else None, checked_at=ts,
                                )
                            else:
                                store.update_health_direct(
                                    node.id, tcp_ok=tcp_ok, tcp_ms=tcp_ms,
                                    http_ok=http_ok, http_ms=http_ms, checked_at=ts,
                                )
            if http_ok and http_ms is not None:
                with self._write_lock:
                    if stop_event is not None and stop_event.is_set():
                        return
                    store.record_latency(node.id, http_ms)   # NN4: HTTPS-handshake latency trend

    def _tick(self, stop_event: threading.Event | None = None) -> None:
        self.run_once(stop_event)
        if self._after_tick is not None and (stop_event is None or not stop_event.is_set()):
            self._after_tick()

    # --- loop lifecycle ---
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop_event = threading.Event()
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="health-monitor")
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        with self._write_lock:
            self._stop_event.set()
        task = self._task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        future = self._future
        if future is not None and not future.done():
            try:
                await asyncio.wait_for(asyncio.wrap_future(future), self.SHUTDOWN_TIMEOUT)
            except TimeoutError:
                pass
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
        self._task = None
        self._future = None
        self._executor = None

    def _safe_tick(self, stop_event: threading.Event | None = None) -> None:
        # one bad sweep (store error, malformed node row) must never kill the loop — log & continue,
        # else the whole health sweep stops forever and failover runs on frozen data.
        try:
            self._tick(stop_event)
        except Exception as exc:
            log.exception("health monitor sweep failed")
            self._errors.record(self._state.store, f"health sweep failed: {exc}")

    async def _loop(self) -> None:
        # run one sweep immediately so health engages right after start instead of serving the
        # stale pre-restart snapshot for a full interval (up to 30 min); then keep going. NOTHING
        # short of cancellation may end this task: the sweep is what keeps standby health fresh
        # enough for auto-failover to have a candidate.
        assert self._executor is not None
        while True:
            try:
                self._future = self._executor.submit(self._safe_tick, self._stop_event)
                await asyncio.wrap_future(self._future)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("health monitor tick could not run", exc_info=True)
                self._errors.record(self._state.store, f"health sweep could not run: {exc}")
            if self._stop_event.is_set():
                return
            await asyncio.sleep(self._interval())
