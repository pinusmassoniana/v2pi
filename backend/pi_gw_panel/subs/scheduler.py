import asyncio
import concurrent.futures
import datetime
import logging
import queue
import random
import threading
import time

from pi_gw_panel.subs import service

log = logging.getLogger("pi_gw_panel")

# One refresh is a 20 s fetch deadline plus a bounded parse/reconcile. Anything past this is a
# wedged worker, not a slow provider — collecting it without a timeout would pin the tick
# (and, through it, shutdown) on a single subscription forever. This is one deadline for the
# whole batch, not per subscription: four sequential per-future waits let a single tick run for
# four times this long.
REFRESH_TIMEOUT = 120.0

# Shutdown waits for live refreshes so the shared store isn't closed underneath them, but it
# does not wait forever: a wedged worker must not hold the panel's shutdown open. Stragglers
# past this point are logged and left; whatever they touch afterwards fails loudly instead.
SHUTDOWN_TIMEOUT = 30.0


class _DaemonPool:
    """A fixed-size executor whose workers are *daemon* threads.

    `ThreadPoolExecutor` cannot be used here. Its futures cannot be cancelled once running, and
    since 3.9 its workers are non-daemon threads that it joins from a
    `threading._register_atexit` hook — so a wedged refresh keeps the interpreter alive at exit
    no matter how carefully the pool is shut down, and no bounded wait in `_drain` can help.
    (Flipping `daemon` on those threads is not an option either: it cannot be set after
    `start()`, and the atexit hook joins them explicitly regardless.)

    Daemon threads are not joined at interpreter exit, which is the only thing that actually
    bounds shutdown. The residual is that a wedged worker is killed mid-flight during teardown
    and whatever it touches afterwards fails loudly — which is exactly the trade `_drain`
    already documents for a straggler it stopped waiting on.

    Only the slice of the `Executor` API this module uses is implemented: `submit` returning a
    real `concurrent.futures.Future`, and `shutdown(wait=…, cancel_futures=…)`.
    """

    def __init__(self, max_workers: int, thread_name_prefix: str = "") -> None:
        self._max_workers = max_workers
        self._prefix = thread_name_prefix
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._shutdown = False

    def submit(self, fn, /, *args, **kwargs) -> concurrent.futures.Future:
        future: concurrent.futures.Future = concurrent.futures.Future()
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            self._queue.put((future, fn, args, kwargs))
            if len(self._threads) < self._max_workers:
                worker = threading.Thread(
                    target=self._work, name=f"{self._prefix}-{len(self._threads)}", daemon=True)
                self._threads.append(worker)
                worker.start()
        return future

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:                      # shutdown sentinel, one per worker
                return
            future, fn, args, kwargs = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn(*args, **kwargs))
            except BaseException as exc:          # mirrors ThreadPoolExecutor's worker
                future.set_exception(exc)
            del future, item                      # don't pin the result until the next job

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._lock:
            self._shutdown = True
            if cancel_futures:
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is not None:
                        item[0].cancel()
            for _ in self._threads:
                self._queue.put(None)
            threads = list(self._threads)
        if wait:
            for worker in threads:
                worker.join()


def _new_pool() -> _DaemonPool:
    return _DaemonPool(max_workers=4, thread_name_prefix="sub-refresh")


def _age_since(last_fetched: str | None) -> float | None:
    """Wall-clock seconds since the persisted ISO ``last_fetched``, or None if missing /
    unparseable. Used to seed due-ness across a restart so we don't refresh-storm."""
    if not last_fetched:
        return None
    try:
        ts = datetime.datetime.fromisoformat(last_fetched)
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds())


class SubScheduler:
    """Background asyncio loop that refreshes subscriptions whose interval has elapsed.
    The blocking refresh is offloaded to a bounded pool so the event loop stays free.
    `interval_sec <= 0` means manual-only (never auto-refreshed)."""

    def __init__(self, state, tick_sec: float = 30.0):
        self._state = state
        self._tick = tick_sec
        self._task: asyncio.Task | None = None
        self._last_run: dict[int, float] = {}
        self._retry_at: dict[int, float] = {}
        self._failures: dict[int, int] = {}
        # One live refresh per subscription. A future stays here until it actually finishes —
        # including one we stopped waiting on — so nothing is ever queued behind a wedged
        # worker and the four-thread pool cannot be exhausted by repeats of one bad feed.
        self._inflight: dict[int, concurrent.futures.Future] = {}
        self._closing = False
        self._pool: _DaemonPool | None = None

    def _forget_deleted(self, known: set) -> None:
        """Drop per-subscription bookkeeping for ids that no longer exist. Ids are never reused
        (`subscriptions.id` is AUTOINCREMENT), so this is memory hygiene, not correctness —
        without it a long-lived panel accumulates an entry per deleted subscription."""
        for cache in (self._last_run, self._retry_at, self._failures):
            for sub_id in [key for key in cache if key not in known]:
                cache.pop(sub_id, None)
        service.prune_refresh_locks(known)

    def due_subs(self, now: float) -> list:
        due = []
        subs = self._state.store.list_subscriptions()
        self._forget_deleted({sub.id for sub in subs})
        for sub in subs:
            if sub.interval_sec <= 0 or not sub.enabled:   # manual-only or paused (N2)
                continue
            if sub.id in self._inflight:
                # A refresh is still running (it holds this subscription's refresh lock, so a
                # second one would only queue behind it and burn a worker). Nothing is due for
                # it — not even a retry — until it lands and `_reap_inflight` scores it.
                continue
            retry_at = self._retry_at.get(sub.id)
            if retry_at is not None:
                if now >= retry_at:
                    due.append(sub)
                continue
            last = self._last_run.get(sub.id)
            if last is None:
                # First sight since start: honor the persisted last_fetched so a restart doesn't
                # refresh every auto-sub at once. Fetched recently → seed _last_run so it next
                # fires when the remaining interval elapses; otherwise it's genuinely due.
                age = _age_since(sub.last_fetched)
                if age is not None and age < sub.interval_sec:
                    self._last_run[sub.id] = now - age
                    continue
                due.append(sub)
            elif (now - last) >= sub.interval_sec:
                due.append(sub)
        return due

    @staticmethod
    def _result_ok(result) -> bool:
        return result is None or result.get("ok", "error" not in result)

    def _score(self, sub_id: int, ok: bool, now: float) -> None:
        """Advance the interval on success, or arm a capped exponential backoff on failure."""
        if ok:
            self._last_run[sub_id] = now
            self._retry_at.pop(sub_id, None)
            self._failures.pop(sub_id, None)
        else:
            failures = self._failures.get(sub_id, 0) + 1
            self._failures[sub_id] = failures
            delay = 30.0 * (2 ** min(failures - 1, 5)) * random.uniform(0.8, 1.2)
            self._retry_at[sub_id] = now + min(900.0, delay)

    def _reap_inflight(self, now: float) -> None:
        """Score and drop the refreshes we stopped waiting on that have since finished.
        A still-running one stays tracked, which is what keeps `due_subs` from re-submitting
        it and what stops a repeatedly-timing-out feed from consuming the whole pool."""
        for sub_id, future in list(self._inflight.items()):
            if not future.done():
                continue
            del self._inflight[sub_id]
            try:
                ok = self._result_ok(future.result())
            except concurrent.futures.CancelledError:   # BaseException — not caught below
                continue
            except Exception:
                log.exception("SubScheduler: refresh failed for subscription %s", sub_id)
                ok = False
            self._score(sub_id, ok, now)

    def run_once(self, now: float) -> None:
        if self._closing:
            return
        self._reap_inflight(now)
        due = self.due_subs(now)
        if not due:
            return
        if self._pool is None:
            self._pool = _new_pool()
        futures = {}
        for sub in due:
            future = self._pool.submit(service.refresh, self._state, sub)
            self._inflight[sub.id] = future
            futures[future] = sub
        deadline = time.monotonic() + REFRESH_TIMEOUT
        for future, sub in futures.items():
            try:
                remaining = max(0.0, deadline - time.monotonic())
                ok = self._result_ok(future.result(timeout=remaining))
            except concurrent.futures.TimeoutError:
                # Abandoned, not cancelled: the worker is still holding this subscription's
                # refresh lock. Leave it in `_inflight` so no retry is armed and no second
                # attempt is submitted — `_reap_inflight` scores it once it actually lands.
                log.error("SubScheduler: refresh for subscription %s is still running past the "
                          "%.0fs batch deadline — leaving it to finish, no retry until it does",
                          sub.id, REFRESH_TIMEOUT)
                continue
            except concurrent.futures.CancelledError:   # BaseException — not caught below
                self._inflight.pop(sub.id, None)
                continue
            except Exception:
                log.exception("SubScheduler: refresh failed for subscription %s", sub.id)
                ok = False
            self._inflight.pop(sub.id, None)
            self._score(sub.id, ok, now)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._closing = False
            if self._pool is None:
                self._pool = _new_pool()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        # Set before cancelling: a `run_once` already handed to the default executor keeps
        # running after the task is cancelled, and must not build a fresh pool behind us.
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._pool is not None:
            pool = self._pool
            self._pool = None
            await asyncio.to_thread(self._drain, pool)

    def _drain(self, pool: _DaemonPool) -> None:
        """Stop taking work, then wait a *bounded* time for the live refreshes.

        At most four remain and each has a 20-second fetch deadline, so waiting is normally
        brief and it keeps AppState from closing the shared store underneath them. But a wedged
        worker never returns, and an unbounded join here made it hold the whole shutdown.

        The bound is the whole point, and it only holds because the pool's workers are daemon
        threads (see `_DaemonPool`): a running future cannot be cancelled, so with a stdlib
        `ThreadPoolExecutor` the wedged worker was still joined at interpreter exit and the
        panel's *process* hung anyway, however promptly this returned. What is bounded now is
        the whole shutdown: at most `SHUTDOWN_TIMEOUT` here, and nothing after it — the wedged
        worker is abandoned and the interpreter exits over it."""
        pool.shutdown(wait=False, cancel_futures=True)
        deadline = time.monotonic() + SHUTDOWN_TIMEOUT
        for sub_id, future in list(self._inflight.items()):
            try:
                future.result(timeout=max(0.0, deadline - time.monotonic()))
            except concurrent.futures.TimeoutError:
                log.warning("SubScheduler: refresh for subscription %s did not finish within "
                            "%.0fs of shutdown — no longer waiting for it", sub_id,
                            SHUTDOWN_TIMEOUT)
            except (concurrent.futures.CancelledError, Exception):
                pass
        self._inflight.clear()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._tick)
            try:
                now = time.monotonic()
                await asyncio.get_running_loop().run_in_executor(None, self.run_once, now)
            except asyncio.CancelledError:   # stop() cancels the task — let it unwind
                raise
            except Exception:                # P1: one bad tick must not kill the loop forever
                log.exception("SubScheduler: refresh tick failed; continuing")
