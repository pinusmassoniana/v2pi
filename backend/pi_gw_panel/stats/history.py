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
            ts_ms = int(ts_ms)
            if self._buf and ts_ms < self._buf[-1][0]:      # NTP step moved wall-clock back → clamp so the series stays monotonic
                ts_ms = self._buf[-1][0]
            self._buf.append((ts_ms, round(up_bps), round(down_bps)))

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
                 running=lambda: True, clock=lambda: time.time(),
                 on_total=None, flush_interval=30.0, on_minute=None):
        self._sampler = sampler
        self._history = history
        self._stats_enabled = stats_enabled        # callable -> bool
        self._running = running                    # callable -> bool (xray up? — F5 gate)
        self._interval_ms = interval_ms            # callable -> int
        self._clock = clock
        self._task: asyncio.Task | None = None
        # audit F: persist cumulative proxy bytes so "data used" survives an xray restart.
        # `on_total(up_delta, down_delta)` adds to a durable counter; deltas are batched and
        # flushed at most every `flush_interval` s to spare the SD card.
        self._on_total = on_total
        self._flush_interval = flush_interval
        self._prev_abs: dict | None = None         # last absolute proxy counters seen
        self._pending = {"up": 0, "down": 0}
        self._last_flush = 0.0
        # N4: `on_minute(ts_min, up_bytes, down_bytes)` persists a 1-min downsample of the
        # same deltas (one DB write/min) — the durable series behind the 24h/7d windows.
        self._on_minute = on_minute
        self._minute: dict | None = None           # current minute bucket {min, up, down}

    def record_sample(self, out: dict) -> None:
        """Map one sampler output to a history point (proxy outbound, zeros if absent)."""
        p = (out or {}).get("proxy") or {}
        self._history.record(int(self._clock() * 1000),
                             p.get("up_bps", 0.0), p.get("down_bps", 0.0))
        self._accumulate_total()

    def _accumulate_total(self) -> None:
        """Add this tick's proxy-byte delta to the durable counter (reset-safe, batched)."""
        if self._on_total is None and self._on_minute is None:
            return
        tot = (getattr(self._sampler, "totals", {}) or {}).get("proxy")
        if not tot:
            return
        up, down = int(tot.get("up", 0)), int(tot.get("down", 0))
        if self._prev_abs is not None:
            du, dd = up - self._prev_abs["up"], down - self._prev_abs["down"]
            # clamp each direction independently — a lone decreasing series must not overwrite
            # the other direction's small correct delta with the full cumulative absolute (spike)
            du = du if du >= 0 else max(0, up)
            dd = dd if dd >= 0 else max(0, down)
            self._pending["up"] += du
            self._pending["down"] += dd
            self._accumulate_minute(du, dd)
        self._prev_abs = {"up": up, "down": down}
        now = self._clock()
        if (self._pending["up"] or self._pending["down"]) and now - self._last_flush >= self._flush_interval:
            self.flush_total()

    def _accumulate_minute(self, du: int, dd: int) -> None:
        """Bucket this tick's byte delta by wall-clock minute; persist a bucket when the
        minute rolls over (N4). Empty minutes write nothing (gaps stay gaps)."""
        if self._on_minute is None:
            return
        cur = int(self._clock() // 60)
        if self._minute is not None and self._minute["min"] != cur:
            self.flush_minute()
        if du or dd:
            if self._minute is None:
                self._minute = {"min": cur, "up": 0, "down": 0}
            self._minute["up"] += du
            self._minute["down"] += dd

    def flush_minute(self) -> None:
        """Persist and clear the current minute bucket (also called on shutdown). The store
        upsert is additive, so a partial flush + restart within one minute never loses bytes."""
        if self._on_minute is not None and self._minute is not None:
            if self._minute["up"] or self._minute["down"]:
                self._on_minute(self._minute["min"], self._minute["up"], self._minute["down"])
            self._minute = None

    def flush_total(self) -> None:
        """Persist and clear the pending byte deltas (also called on shutdown)."""
        if self._on_total is not None and (self._pending["up"] or self._pending["down"]):
            self._on_total(self._pending["up"], self._pending["down"])
            self._pending = {"up": 0, "down": 0}
        self._last_flush = self._clock()

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
        try:
            self.flush_total()        # don't lose the last batch of data-used on shutdown
            self.flush_minute()
        except Exception:
            log.debug("data-used flush on stop failed", exc_info=True)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        next_t = time.monotonic()                   # fixed-deadline cadence base (monotonic → NTP-immune)
        while True:
            interval = max(0.5, self._interval_ms() / 1000.0)
            try:
                if self._stats_enabled() and self._running():
                    out = await loop.run_in_executor(None, self._sampler.sample)
                    self.record_sample(out)
            except Exception:
                log.debug("traffic history sample failed", exc_info=True)
            # sleep to the next deadline so sample latency doesn't stretch the real period
            next_t += interval
            now = time.monotonic()
            if next_t < now:                        # fell behind (slow sample) → resync, don't burst-catch-up
                next_t = now
            await asyncio.sleep(next_t - now)
