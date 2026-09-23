"""Persisted connection-event log (audit N2, A9): {ts, kind, detail} rows in `conn_events`,
so the panel can answer "why did my egress change / when did it reconnect?" — and, since A9,
"how often did this happen this month?", which the previous 40-entry ring in the settings k/v
could not.

kinds: connect · disconnect · xray-stop · xray-restart · failover · all-nodes-down · stale-active
· rw-revoke · kill-switch · lan-access · ipv6 · health-error

`record()` keeps its old contract to the letter — same signature, and it NEVER raises, because
telemetry may not break a connection action. Only the storage changed.
"""
import logging
import time

log = logging.getLogger("pi_gw_panel")

_CAP = 40      # what `recent()` returns by default: the Network card's feed, unchanged


def record(store, kind: str, detail: str = "", now: float | None = None) -> None:
    """Append an event. Never raises — but a lost event is logged, not dropped unseen."""
    try:
        store.add_event(int(time.time() if now is None else now), kind, detail)
    except Exception:
        log.warning("could not record the %s event (%s)", kind, detail, exc_info=True)


def recent(store, limit: int = _CAP) -> list[dict]:
    """The recorded events, oldest→newest; [] (and a log line) when the store cannot answer."""
    try:
        return store.list_events(limit=limit)
    except Exception:
        log.warning("could not read the connection events", exc_info=True)
        return []


# What an incident is made of. A span opens when the tunnel stops carrying traffic and closes
# when something puts it back; anything else (a kill-switch toggle, a revocation) is history, not
# downtime, and is deliberately not in either set.
DOWN_KINDS = frozenset({"disconnect", "xray-stop", "all-nodes-down", "stale-active"})
UP_KINDS = frozenset({"connect", "failover", "xray-restart"})


def incidents(events: list[dict], now: int) -> list[dict]:
    """Down→up spans, newest first: [{started, ended, seconds, kind, detail, ongoing}].

    An ongoing span is closed at `now` and marked, because "down since 14:02" is the answer
    someone is looking for; a second down event inside an open span does not start a new one.
    """
    spans: list[dict] = []
    open_span: dict | None = None
    for event in events:
        kind, ts = event.get("kind"), int(event.get("ts") or 0)
        if kind in DOWN_KINDS and open_span is None:
            open_span = {"started": ts, "kind": kind, "detail": event.get("detail", "")}
        elif kind in UP_KINDS and open_span is not None:
            open_span.update(ended=ts, seconds=max(0, ts - open_span["started"]), ongoing=False)
            spans.append(open_span)
            open_span = None
    if open_span is not None:
        open_span.update(ended=now, seconds=max(0, now - open_span["started"]), ongoing=True)
        spans.append(open_span)
    return list(reversed(spans))


def downtime(spans: list[dict], since: int) -> int:
    """Seconds of downtime in the window, counting only the part that falls inside it."""
    total = 0
    for span in spans:
        start, end = max(span["started"], since), span["ended"]
        if end > start:
            total += end - start
    return total
