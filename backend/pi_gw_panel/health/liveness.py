"""Fast connection-resilience loop (audit B1 + B2), distinct from the slow 30-min
all-node HealthMonitor sweep.

Every `interval_sec` (default 20 s) it runs the **watchdog** (restart a crashed xray) and
**failover** evaluation; every `probe_interval` (default 60 s) it **real-probes the active node**
to advance `fail_count` for fast failover.

The active-node probe spins a **separate throwaway xray** — it must NOT go through the live
tunnel's proxy: routing the probe (incl. the v6-egress request) through the live connection
heavily degrades the user's active connection for the duration of the check. The cost is a
short-lived xray per probe, kept modest by the 60 s probe cadence.

All blocking work runs in an executor; every step is best-effort and never raises out.
"""
import asyncio
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pi_gw_panel.health import probe, failover
from pi_gw_panel.health.monitor import DEFAULT_PROBE_URL
from pi_gw_panel.controller import apply_lock
from pi_gw_panel.net_control import events

log = logging.getLogger("pi_gw_panel")
DEFAULT_INTERVAL = 20.0        # watchdog + failover-eval cadence
DEFAULT_PROBE_INTERVAL = 60.0  # active-node real-probe cadence (a throwaway xray each time)
DEFAULT_PROBE_URL6 = "https://api6.ipify.org?format=json"


class LivenessLoop:
    SHUTDOWN_TIMEOUT = 2.0

    def __init__(self, state, interval_sec: float = DEFAULT_INTERVAL,
                 probe_interval: float = DEFAULT_PROBE_INTERVAL,
                 real_through=probe.real_through_node, failover_run=failover.run,
                 restart=None, now=None, now_iso=None):
        self._state = state
        self._interval = interval_sec
        self._probe_interval = probe_interval
        self._real_through = real_through
        self._failover_run = failover_run
        self._restart = restart or (lambda st: st.supervisor.start())
        self._now = now or time.time
        self._now_iso = now_iso or (lambda: datetime.now(timezone.utc).isoformat())
        self._last_probe = 0.0
        self._task: asyncio.Task | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._future: Future | None = None
        self._stop_event = threading.Event()
        self._write_lock = threading.Lock()

    # --- B1: restart a crashed xray ---
    def _watchdog(self, stop_event: threading.Event | None = None) -> None:
        st = self._state
        if stop_event is not None and stop_event.is_set():
            return
        if st.supervisor.state() != "error":      # 'error' = wanted-running but the proc died
            return
        with apply_lock:                           # serialize vs apply / a deliberate stop
            with self._write_lock:
                if ((stop_event is not None and stop_event.is_set()) or
                        st.supervisor.state() != "error"):
                    return
                self._restart(st)
                events.record(st.store, "xray-restart", "auto-restarted after crash", now=self._now())
                log.warning("watchdog: xray was down — restarted")

    # --- B2: real-probe the active node so fail_count is responsive (SEPARATE xray) ---
    def _probe_active(self, stop_event: threading.Event | None = None) -> None:
        st = self._state
        store = st.store
        if ((stop_event is not None and stop_event.is_set()) or
                (store.get_setting("health_enabled") or "1") != "1"):
            return
        with apply_lock:
            av = store.get_setting("active_node_id")
            aid = int(av) if av else None
            if aid is None or not st.supervisor.status().get("running"):
                return
            node = store.get_node(aid)
            if node is None:
                return
        probe_url = store.get_setting("health_probe_url") or DEFAULT_PROBE_URL
        url6 = (store.get_setting("health_probe_url6") or DEFAULT_PROBE_URL6
                if (store.get_setting("ipv6_enabled") or "0") == "1" else None)
        # Throwaway xray, NOT the live tunnel — isolating the probe keeps the user's active
        # connection undisturbed during the check (and tunneled_fetch no longer matters here).
        real_ok, real_ms, egress, egress6 = self._real_through(node, st.xray_bin, probe_url,
                                                               probe_url6=url6)
        # a manual switch may have moved the active node while this (seconds-long) probe ran — don't
        # attribute the result or advance fail_count on a node that is no longer active.
        with apply_lock:
            with self._write_lock:
                if stop_event is not None and stop_event.is_set():
                    return
                cur = store.get_setting("active_node_id")
                if (int(cur) if cur else None) != aid:
                    return
                store.update_health_real(
                    aid, real_ok=real_ok, real_ms=real_ms, egress_ip=egress,
                    egress_ip6=egress6, checked_at=self._now_iso(),
                )
        if real_ok and real_ms is not None:
            with apply_lock:
                with self._write_lock:
                    if stop_event is not None and stop_event.is_set():
                        return
                    cur = store.get_setting("active_node_id")
                    if (int(cur) if cur else None) == aid:
                        store.record_latency(aid, real_ms)

    def _tick(self, stop_event: threading.Event | None = None) -> None:
        try:
            self._watchdog(stop_event)
        except Exception:
            log.debug("liveness watchdog failed", exc_info=True)
        if ((stop_event is None or not stop_event.is_set()) and
                self._now() - self._last_probe >= self._probe_interval):
            self._last_probe = self._now()
            try:
                self._probe_active(stop_event)
            except Exception:
                log.debug("liveness probe failed", exc_info=True)
        try:
            if stop_event is not None and stop_event.is_set():
                return
            new_active = self._failover_run(self._state, self._now())
            if new_active is not None:
                events.record(self._state.store, "failover",
                              f"auto-failover to node {new_active}", now=self._now())
        except Exception:
            # failover is safety-critical — a persistent bug here means the user THINKS they're
            # protected while auto-failover never fires. Surface it (warning), not debug.
            log.warning("failover tick failed", exc_info=True)

    # --- loop lifecycle (mirrors HealthMonitor) ---
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop_event = threading.Event()
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="health-liveness")
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

    async def _loop(self) -> None:
        # tick immediately on start so the watchdog can revive a crashed xray right away instead of
        # leaving it down for the first interval (_tick is already fully exception-guarded).
        assert self._executor is not None
        self._future = self._executor.submit(self._tick, self._stop_event)
        await asyncio.wrap_future(self._future)
        while True:
            await asyncio.sleep(self._interval)
            self._future = self._executor.submit(self._tick, self._stop_event)
            await asyncio.wrap_future(self._future)
