"""Turn xray's cumulative byte counters into per-outbound bit/s rates (Wave 3a).

`query_fn() -> {stat_name: value}` (e.g. StatsClient.query); `clock() -> monotonic
seconds`. The first sample has no prior snapshot → all rates 0. A counter that drops
(xray restart / reset) yields 0 for that series rather than a negative spike."""
import time


def _parse(name: str):
    """'outbound>>>{tag}>>>traffic>>>{up,down}link' → (tag, direction); else (None, None)."""
    parts = name.split(">>>")
    if len(parts) == 4 and parts[0] == "outbound" and parts[2] == "traffic":
        return parts[1], parts[3]
    return None, None


class TrafficSampler:
    def __init__(self, query_fn, clock=time.monotonic):
        self._query = query_fn
        self._clock = clock
        self._prev: dict[str, int] = {}
        self._prev_t: float | None = None

    def sample(self) -> dict[str, dict[str, float]]:
        now = self._clock()
        counters = self._query()
        dt = (now - self._prev_t) if self._prev_t is not None else 0.0
        out: dict[str, dict[str, float]] = {}
        for name, value in counters.items():
            tag, direction = _parse(name)
            if tag is None:
                continue
            prev = self._prev.get(name)
            if dt > 0 and prev is not None and value >= prev:
                bps = (value - prev) * 8 / dt          # bytes → bits per second
            else:
                bps = 0.0                              # first sample / reset / rollover
            entry = out.setdefault(tag, {"up_bps": 0.0, "down_bps": 0.0})
            entry["up_bps" if direction == "uplink" else "down_bps"] = bps
        self._prev = counters
        self._prev_t = now
        return out
