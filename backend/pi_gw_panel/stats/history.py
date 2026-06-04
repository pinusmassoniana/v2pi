"""Rolling in-memory history of proxy throughput, fed by an always-on background
sampler (TrafficRecorder) so the Dashboard graph shows a full window the instant it
opens and survives navigation/reload.

The recorder owns its OWN TrafficSampler instance, so it never contends with the
live-WS sampler (each keeps independent prev-counters; the xray counters are
cumulative, so two readers compute correct deltas independently). The buffer is a
bounded deque → O(1) append, fixed memory."""
import asyncio
import logging
import threading
import time
from collections import deque

log = logging.getLogger("pi_gw_panel")


def _downsample(items: list, n: int) -> list:
    """Evenly stride `items` down to ~n points, always keeping the most recent one."""
    if n <= 0 or len(items) <= n:
        return items
    step = len(items) / n
    out = [items[int(i * step)] for i in range(n)]
    out[-1] = items[-1]
    return out


class TrafficHistory:
    """Thread-safe bounded ring buffer of (ts_ms, up_bps, down_bps) integer tuples.

    Written by the recorder thread (run_in_executor) and read by sync REST handlers
    (Starlette thread-pool), so every access takes a cheap lock."""

    def __init__(self, maxlen: int = 3600):
        self._buf: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def record(self, ts_ms: int, up_bps: float, down_bps: float) -> None:
        with self._lock:
            self._buf.append((int(ts_ms), round(up_bps), round(down_bps)))

    def series(self, since_ms: int | None = None, max_points: int | None = None) -> list:
        with self._lock:
            items = list(self._buf)
        if since_ms is not None:
            items = [s for s in items if s[0] >= since_ms]
        if max_points:
            items = _downsample(items, max_points)
        return items

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)


class TrafficRecorder:
    """Always-on async background task: every interval, sample proxy throughput and
    append it to a TrafficHistory. Gated on stats_enabled; never raises out of the loop.

    `sampler` is a TrafficSampler (its own instance), `stats_enabled`/`interval_ms` are
    callables read live so a settings change takes effect without a restart."""

    def __init__(self, sampler, history: TrafficHistory, stats_enabled, interval_ms,
                 clock=lambda: time.time()):
        self._sampler = sampler
        self._history = history
        self._stats_enabled = stats_enabled        # callable -> bool
        self._interval_ms = interval_ms            # callable -> int
        self._clock = clock
        self._task: asyncio.Task | None = None

    def record_sample(self, out: dict) -> None:
        """Map one sampler output to a history point (proxy outbound, zeros if absent)."""
        p = (out or {}).get("proxy") or {}
        self._history.record(int(self._clock() * 1000),
                             p.get("up_bps", 0.0), p.get("down_bps", 0.0))

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            interval = max(0.5, self._interval_ms() / 1000.0)
            try:
                if self._stats_enabled():
                    out = await loop.run_in_executor(None, self._sampler.sample)
                    self.record_sample(out)
            except Exception:
                log.debug("traffic history sample failed", exc_info=True)
            await asyncio.sleep(interval)
