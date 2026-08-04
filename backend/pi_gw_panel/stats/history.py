"""Rolling in-memory history of proxy throughput, fed by an always-on background
sampler (TrafficRecorder) so the Dashboard graph shows a full window the instant it
opens and survives navigation/reload.

The recorder is the sole TrafficSampler owner. WebSockets read its immutable latest
snapshot, so extra tabs never multiply gRPC calls or disturb delta baselines. The
buffer is a bounded deque → O(1) append, fixed memory."""
import asyncio
import contextlib
import copy
import logging
import threading
import time
from collections import deque

log = logging.getLogger("pi_gw_panel")


def bounded_interval_ms(value, default: int = 1000) -> int:
    """One cadence contract for settings, recorder, REST metadata, and WebSockets."""
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return min(60_000, max(500, parsed))


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

    def record(self, ts_ms: int, up_bps: float, down_bps: float) -> int:
        """Append one point; returns the timestamp actually stored (see the clamp below), so
        callers bucket their own accounting on the same value the series uses."""
        with self._lock:
            ts_ms = int(ts_ms)
            if self._buf and ts_ms < self._buf[-1][0]:      # NTP step moved wall-clock back → clamp so the series stays monotonic
                ts_ms = self._buf[-1][0]
            self._buf.append((ts_ms, round(up_bps), round(down_bps)))
            return ts_ms

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

    # A minute bucket is ~40 bytes; 1440 of them is a full day of writer downtime retained
    # before the oldest starts falling off. Bounded so a permanently failing DB write can no
    # longer grow the queue by one entry per minute until the process is OOM-killed.
    _MAX_PENDING_MINUTES = 1440
    _WARN_INTERVAL = 60.0        # per failure kind — these fire on every tick while broken

    def __init__(self, sampler, history: TrafficHistory, stats_enabled, interval_ms,
                 running=lambda: True, clock=lambda: time.time(),
                 on_total=None, flush_interval=30.0, on_minute=None,
                 baseline=None, on_baseline=None, transaction=None):
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
        # `baseline`/`on_baseline` persist those absolute counters across a panel restart: with
        # an in-memory-only baseline the first post-restart tick recorded a zero delta, so every
        # restart silently dropped one interval of "data used". The stored value is only ever
        # advanced after the deltas it covers have been handed to `on_total`.
        self._prev_abs: dict | None = None         # last absolute proxy counters seen
        if baseline:
            self._prev_abs = {"up": int(baseline["up"]), "down": int(baseline["down"])}
        self._on_baseline = on_baseline
        # The gap a restored baseline spans belongs to the downtime, not to the minute the panel
        # came back in — count it once in the lifetime total and leave the per-minute gap a gap.
        self._recovered = self._prev_abs is not None
        self._pending = {"up": 0, "down": 0}
        self._last_flush = 0.0
        # Caller-supplied context-manager factory (e.g. `store.transaction`) so flush_total can
        # wrap the baseline write and the total write in ONE store transaction: a callback
        # failure or a crash between the two must roll back both, never commit the baseline
        # without the bytes it covers (audit FIX-E-1). None (the default) preserves the old
        # untransacted behaviour for callers/tests that don't share a real store.
        self._transaction = transaction
        self._warned: dict = {}                    # failure kind -> monotonic ts of last warning
        # N4: `on_minute(ts_min, up_bytes, down_bytes)` persists a 1-min downsample of the
        # same deltas (one DB write/min) — the durable series behind the 24h/7d windows.
        self._on_minute = on_minute
        self._minute: dict | None = None           # current minute bucket {min, up, down}
        self._pending_minutes: deque = deque()     # completed buckets retained until callback succeeds
        self._latest: dict | None = None
        self._latest_error = ""
        self._latest_lock = threading.Lock()

    def _warn(self, kind: str, msg: str) -> None:
        """Surface a failure at a level that actually reaches a handler, at most once a minute
        per kind. These paths fire on every tick while the DB is unwritable, and an unthrottled
        warning would rotate the 5 MB app log clean of everything else within hours."""
        now = time.monotonic()
        if now - self._warned.get(kind, float("-inf")) >= self._WARN_INTERVAL:
            self._warned[kind] = now
            log.warning(msg, exc_info=True)
        else:
            log.debug(msg, exc_info=True)

    def record_sample(self, out: dict) -> None:
        """Map one sampler output to a history point (proxy outbound, zeros if absent)."""
        p = (out or {}).get("proxy") or {}
        # The series clamps a backward clock step; take the clamped value back so the byte
        # accounting buckets on exactly the timestamp the graph shows.
        ts_ms = self._history.record(int(self._clock() * 1000),
                                     p.get("up_bps", 0.0), p.get("down_bps", 0.0))
        now = ts_ms / 1000.0
        with self._latest_lock:
            self._latest = {
                "ts": ts_ms,
                "outbounds": copy.deepcopy(out or {}),
                "totals": copy.deepcopy(getattr(self._sampler, "totals", {}) or {}),
            }
            self._latest_error = ""
        self._accumulate_total(now)

    def record_error(self, exc: Exception | str) -> None:
        with self._latest_lock:
            self._latest_error = str(exc) or type(exc).__name__

    def snapshot(self) -> dict:
        """Return a detached frame; consumers can never mutate the producer's snapshot."""
        with self._latest_lock:
            latest = copy.deepcopy(self._latest) if self._latest is not None else None
            error = self._latest_error
        if latest is None:
            return {"error": error or "stats sample pending", "stale": True}
        if error:
            latest["error"] = error
            latest["stale"] = True
        else:
            latest["stale"] = False
        return latest

    def _accumulate_total(self, now: float) -> None:
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
            # Advance the absolute baseline before any fallible persistence callback. A failed
            # flush can therefore be retried, but this sample's delta is never accepted twice.
            self._prev_abs = {"up": up, "down": down}
            if self._on_total is not None:
                self._pending["up"] += du
                self._pending["down"] += dd
            if self._recovered:
                # First delta after a restart: it spans the downtime, so charging it to the
                # minute the panel came back in would draw a spike that never happened.
                self._recovered = False
            else:
                self._accumulate_minute(du, dd, now)
        else:
            self._prev_abs = {"up": up, "down": down}
        if (self._pending["up"] or self._pending["down"]) and now - self._last_flush >= self._flush_interval:
            try:
                self.flush_total()
            except Exception:
                self._warn("total", "data-used total flush failed; retained for retry")
        try:
            self.flush_minute(include_current=False)
        except Exception:
            self._warn("minute", "traffic-minute flush failed; retained for retry")

    def _accumulate_minute(self, du: int, dd: int, now: float) -> None:
        """Bucket this tick's byte delta by wall-clock minute; persist a bucket when the
        minute rolls over (N4). Empty minutes write nothing (gaps stay gaps)."""
        if self._on_minute is None:
            return
        cur = int(now // 60)
        if self._minute is not None and self._minute["min"] != cur:
            if self._minute["up"] or self._minute["down"]:
                self._queue_minute(self._minute)
            self._minute = None
        if du or dd:
            if self._minute is None:
                self._minute = {"min": cur, "up": 0, "down": 0}
            self._minute["up"] += du
            self._minute["down"] += dd

    def _queue_minute(self, bucket: dict) -> None:
        """Enqueue one completed bucket, dropping the oldest once the backlog is full.

        Unbounded retention turned a persistently failing DB write into unbounded memory
        growth; the drop is logged (not silently swallowed) because it is real data loss."""
        while len(self._pending_minutes) >= self._MAX_PENDING_MINUTES:
            dropped = self._pending_minutes.popleft()
            log.warning("traffic-minute backlog full (%d buckets); dropping minute %s "
                        "(%d/%d bytes) — the durable series will have a gap",
                        self._MAX_PENDING_MINUTES, dropped["min"], dropped["up"], dropped["down"])
        self._pending_minutes.append(bucket)

    def flush_minute(self, *, include_current: bool = True) -> None:
        """Persist completed buckets in order, removing each only after callback success."""
        if self._on_minute is None:
            return
        if include_current and self._minute is not None:
            if self._minute["up"] or self._minute["down"]:
                self._queue_minute(self._minute)
            self._minute = None
        while self._pending_minutes:
            minute = self._pending_minutes[0]
            self._on_minute(minute["min"], minute["up"], minute["down"])
            self._pending_minutes.popleft()

    def flush_total(self) -> None:
        """Persist and clear the pending byte deltas (also called on shutdown).

        Baseline and total are written inside ONE `self._transaction()` (when the caller
        supplied one) instead of two independent commits: the earlier "baseline first" ordering
        alone only bounded which side under-counted on failure — a crash or callback exception
        landing between the two separate writes still let the baseline commit durably while the
        bytes it covers were never added, losing that delta forever. With a shared transaction,
        either both land or neither does, so a failed/interrupted flush is always safe to retry
        (audit FIX-E-1)."""
        do_baseline = self._on_baseline is not None and self._prev_abs is not None
        do_total = self._on_total is not None and (self._pending["up"] or self._pending["down"])
        if not do_baseline and not do_total:
            self._last_flush = self._clock()
            return
        ctx = self._transaction() if self._transaction is not None else contextlib.nullcontext()
        with ctx:
            if do_baseline:
                self._on_baseline(self._prev_abs["up"], self._prev_abs["down"])
            if do_total:
                self._on_total(self._pending["up"], self._pending["down"])
        # Clear the in-memory delta only after the `with` block above returns without raising.
        # The real SQLite commit for a shared transaction happens in `NodeStore`'s context
        # manager __exit__ (nodes/store.py), which runs at the end of that `with` block — so
        # resetting `_pending` inside the block (as before) zeroed memory before the commit was
        # known to succeed. A commit failure then rolled storage back but left memory at zero,
        # losing the bytes for good instead of leaving them for the next flush to retry
        # (audit FIX-J-2, the remaining half of FIX-E-1).
        if do_total:
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
            self.flush_minute(include_current=True)
        except Exception:
            self._warn("stop", "data-used flush on stop failed")

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        next_t = time.monotonic()                   # fixed-deadline cadence base (monotonic → NTP-immune)
        while True:
            interval = 1.0
            try:
                interval = bounded_interval_ms(self._interval_ms()) / 1000.0
                if self._stats_enabled():
                    if self._running():
                        out = await loop.run_in_executor(None, self._sampler.sample)
                        self.record_sample(out)
                    else:
                        self.record_error("xray is not running")
            except Exception as exc:
                self.record_error(exc)
                self._warn("sample", "traffic history sample failed")
            # sleep to the next deadline so sample latency doesn't stretch the real period
            next_t += interval
            now = time.monotonic()
            if next_t < now:                        # fell behind (slow sample) → resync, don't burst-catch-up
                next_t = now
            await asyncio.sleep(next_t - now)
