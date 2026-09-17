"""B1: how much each pinned device actually moved.

xray's own stats are per outbound, and a tproxy'd packet carries no user, so the panel could not
answer "which device is using the bandwidth" at all. The nft ruleset now carries one counter pair
per reservation (`net_control.render._accounting`); this samples them once a minute, turns the
readings into per-minute deltas and stores them next to the gateway-wide traffic history.

Only PINNED devices are counted (owner decision, 2026-09-17): the counters exist because someone
asked for that device by name, so there is no per-lease churn to clean up and the ruleset of a
gateway that pinned nothing is unchanged.

The two things that make this honest:

  * a counter that went DOWN was reset — every net apply reloads the table — so the reading is
    taken as the delta rather than subtracted into a negative;
  * a device whose reservation is gone stops being sampled, and its history stays until the
    retention window drops it, because "what did the TV use last week" is still a fair question
    after the TV was unpinned.
"""
import asyncio
import logging
import time

from pi_gw_panel.net_control.render import counter_names

log = logging.getLogger(__name__)

SAMPLE_SEC = 60.0


def deltas(previous: dict[str, tuple[int, int]], current: dict[str, tuple[int, int]]) -> dict[str, int]:
    """Bytes added per counter since the previous reading. A counter that is new or that went
    backwards (the table was reloaded) contributes its whole current value, which is what the
    kernel has actually counted since the reset."""
    out: dict[str, int] = {}
    for name, (_packets, total) in current.items():
        before = previous.get(name)
        moved = total if before is None or total < before[1] else total - before[1]
        if moved > 0:
            out[name] = moved
    return out


class DeviceSampler:
    """One reading a minute, bucketed by wall-clock minute like the gateway's own traffic."""

    def __init__(self, state, interval_sec: float = SAMPLE_SEC):
        self._state = state
        self._interval = interval_sec
        self._task: asyncio.Task | None = None
        self._previous: dict[str, tuple[int, int]] = {}

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

    def sample_once(self, now: float | None = None) -> int:
        """Read the counters and persist this minute's bytes per device. Returns the rows written."""
        read = getattr(self._state.net, "read_counters", None)
        if read is None:
            return 0
        current = read()
        moved = deltas(self._previous, current)
        self._previous = current
        if not moved:
            return 0
        ts_min = int((time.time() if now is None else now) // 60)
        rows = 0
        for reservation in self._state.store.list_reservations():
            up_name, down_name = counter_names(reservation.ip)
            up, down = moved.get(up_name, 0), moved.get(down_name, 0)
            if up or down:
                self._state.store.add_device_minute(reservation.ip, ts_min, up, down)
                rows += 1
        return rows

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await asyncio.get_running_loop().run_in_executor(None, self.sample_once)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("device sampling failed; will retry on the next tick")
