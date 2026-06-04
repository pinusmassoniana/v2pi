"""Fast connection-resilience loop (audit B1 + B2), distinct from the slow 30-min
all-node HealthMonitor sweep.

Every `interval_sec` (default 30 s) it:
  1. **Watchdog** — if xray was meant to be running but crashed, restart it immediately
     (the HealthMonitor never touched the supervisor, so a crash otherwise black-holed
     all client traffic for up to ~90 min, or forever with a single node).
  2. **Active real-probe** — real-request through the active node so its `fail_count`
     advances in seconds, not the monitor's 30-min cadence.
  3. **Failover** — act on the fresh counter (this loop is now the failover driver, so it
     can record the event); cooldown still debounces.

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
DEFAULT_INTERVAL = 30.0


class LivenessLoop:
    def __init__(self, state, interval_sec: float = DEFAULT_INTERVAL,
                 real_request=probe.real_request, failover_run=failover.run,
                 restart=None, now=None, now_iso=None):
        self._state = state
        self._interval = interval_sec
        self._real_request = real_request
        self._failover_run = failover_run
        self._restart = restart or (lambda st: st.supervisor.start())
        self._now = now or time.time
        self._now_iso = now_iso or (lambda: datetime.now(timezone.utc).isoformat())
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

    # --- B2: fast real-probe of the active node so fail_count is responsive ---
    def _probe_active(self) -> None:
        st = self._state
        store = st.store
        if (store.get_setting("health_enabled") or "1") != "1":
            return
        av = store.get_setting("active_node_id")
        aid = int(av) if av else None
        if aid is None or not st.supervisor.status().get("running"):
            return
        if (store.get_setting("tunneled_fetch") or "1") != "1":
            return   # no local proxy inbound to probe through → would false-fail the node (NC1)
        probe_url = store.get_setting("health_probe_url") or DEFAULT_PROBE_URL
        proxy_url = f"http://127.0.0.1:{st.settings.local_proxy_port}"
        real_ok, _status, real_ms, egress = self._real_request(proxy_url, probe_url)
        prev = store.get_health(aid)
        store.upsert_health(NodeHealth(
            node_id=aid,
            last_tcp_ok=prev.last_tcp_ok if prev else None,
            last_tcp_ms=prev.last_tcp_ms if prev else None,
            last_http_ok=prev.last_http_ok if prev else None,
            last_http_ms=prev.last_http_ms if prev else None,
            last_real_ok=real_ok, last_real_ms=real_ms, egress_ip=egress,
            checked_at=self._now_iso(),
            fail_count=0 if real_ok else (prev.fail_count if prev else 0) + 1))
        if real_ok and real_ms is not None:
            store.record_latency(aid, real_ms)

    def _tick(self) -> None:
        for step in (self._watchdog, self._probe_active):
            try:
                step()
            except Exception:
                log.debug("liveness step failed", exc_info=True)
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
