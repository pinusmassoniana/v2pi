import asyncio
import concurrent.futures
import datetime
import logging
import random
import time

from pi_gw_panel.subs import service

log = logging.getLogger("pi_gw_panel")


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
        self._pool: concurrent.futures.ThreadPoolExecutor | None = None

    def due_subs(self, now: float) -> list:
        due = []
        for sub in self._state.store.list_subscriptions():
            if sub.interval_sec <= 0 or not sub.enabled:   # manual-only or paused (N2)
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

    def run_once(self, now: float) -> None:
        due = self.due_subs(now)
        if not due:
            return
        if self._pool is None:
            self._pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="sub-refresh")
        futures = {self._pool.submit(service.refresh, self._state, sub): sub for sub in due}
        for future, sub in futures.items():
            try:
                result = future.result()
                ok = result is None or result.get("ok", "error" not in result)
            except Exception:
                log.exception("SubScheduler: refresh failed for subscription %s", sub.id)
                ok = False
            if ok:
                self._last_run[sub.id] = now
                self._retry_at.pop(sub.id, None)
                self._failures.pop(sub.id, None)
            else:
                failures = self._failures.get(sub.id, 0) + 1
                self._failures[sub.id] = failures
                delay = 30.0 * (2 ** min(failures - 1, 5)) * random.uniform(0.8, 1.2)
                self._retry_at[sub.id] = now + min(900.0, delay)

    def start(self) -> None:
        if self._task is None or self._task.done():
            if self._pool is None:
                self._pool = concurrent.futures.ThreadPoolExecutor(
                    max_workers=4, thread_name_prefix="sub-refresh")
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
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
            # At most four live fetches remain and each has a 20-second wall deadline. Wait for
            # them before AppState closes the shared store; queued refreshes are cancelled.
            await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)

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
