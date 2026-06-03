import asyncio
import time
from pi_gw_panel.subs import service


class SubScheduler:
    """Background asyncio loop that refreshes subscriptions whose interval has elapsed.
    The blocking refresh (urllib) is offloaded to a thread so the event loop stays free.
    `interval_sec <= 0` means manual-only (never auto-refreshed)."""

    def __init__(self, state, tick_sec: float = 30.0):
        self._state = state
        self._tick = tick_sec
        self._task: asyncio.Task | None = None
        self._last_run: dict[int, float] = {}

    def due_subs(self, now: float) -> list:
        due = []
        for sub in self._state.store.list_subscriptions():
            if sub.interval_sec <= 0:
                continue
            last = self._last_run.get(sub.id)
            if last is None or (now - last) >= sub.interval_sec:
                due.append(sub)
        return due

    def run_once(self, now: float) -> None:
        for sub in self.due_subs(now):
            service.refresh(self._state, sub)
            self._last_run[sub.id] = now

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
            await asyncio.sleep(self._tick)
            now = time.monotonic()
            await asyncio.get_running_loop().run_in_executor(None, self.run_once, now)
