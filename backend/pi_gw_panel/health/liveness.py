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
import time
from datetime import datetime, timezone
from pi_gw_panel.models import NodeHealth
from pi_gw_panel.health import probe, failover
from pi_gw_panel.health.monitor import DEFAULT_PROBE_URL
from pi_gw_panel.controller import apply_lock
from pi_gw_panel.net_control import events

log = logging.getLogger("pi_gw_panel")
DEFAULT_INTERVAL = 20.0        # watchdog + failover-eval cadence
DEFAULT_PROBE_INTERVAL = 60.0  # active-node real-probe cadence (a throwaway xray each time)
DEFAULT_PROBE_URL6 = "https://api6.ipify.org?format=json"


class LivenessLoop:
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

    # --- B1: restart a crashed xray ---
    def _watchdog(self) -> None:
        st = self._state
        if st.supervisor.state() != "error":      # 'error' = wanted-running but the proc died
            return
        with apply_lock:                           # serialize vs apply / a deliberate stop
            if st.supervisor.state() != "error":   # re-check: a stop may have raced in
                return
            self._restart(st)
            events.record(st.store, "xray-restart", "auto-restarted after crash", now=self._now())
            log.warning("watchdog: xray was down — restarted")

    # --- B2: real-probe the active node so fail_count is responsive (SEPARATE xray) ---
    def _probe_active(self) -> None:
        st = self._state
        store = st.store
        if (store.get_setting("health_enabled") or "1") != "1":
            return
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
        prev = store.get_health(aid)
        store.upsert_health(NodeHealth(
            node_id=aid,
            last_tcp_ok=prev.last_tcp_ok if prev else None,
            last_tcp_ms=prev.last_tcp_ms if prev else None,
            last_http_ok=prev.last_http_ok if prev else None,
            last_http_ms=prev.last_http_ms if prev else None,
            last_real_ok=real_ok, last_real_ms=real_ms, egress_ip=egress, egress_ip6=egress6,
            checked_at=self._now_iso(),
            fail_count=0 if real_ok else (prev.fail_count if prev else 0) + 1))
        if real_ok and real_ms is not None:
            store.record_latency(aid, real_ms)

    def _tick(self) -> None:
        try:
            self._watchdog()
        except Exception:
            log.debug("liveness watchdog failed", exc_info=True)
        if self._now() - self._last_probe >= self._probe_interval:   # rate-limit the spawn
            self._last_probe = self._now()
            try:
                self._probe_active()
            except Exception:
                log.debug("liveness probe failed", exc_info=True)
        try:
            new_active = self._failover_run(self._state, self._now())
            if new_active is not None:
                events.record(self._state.store, "failover",
                              f"auto-failover to node {new_active}", now=self._now())
        except Exception:
            log.debug("failover tick failed", exc_info=True)

    # --- loop lifecycle (mirrors HealthMonitor) ---
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
            await asyncio.sleep(self._interval)
            await asyncio.get_running_loop().run_in_executor(None, self._tick)
