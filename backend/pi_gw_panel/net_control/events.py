"""Bounded, persisted connection-event log (audit N2): a small ring of recent
{ts, kind, detail} entries in the settings k/v, so the Network panel can answer
"why did my egress change / when did it reconnect?" without a new table.

kinds: connect · disconnect · failover · xray-restart · kill-switch · uplink."""
import json
import time

_KEY = "conn_events"
_CAP = 40


def record(store, kind: str, detail: str = "", now: float | None = None) -> None:
    """Append an event (newest last), capped at _CAP. Never raises — telemetry must
    not break a connection action."""
    try:
        ts = int(time.time() if now is None else now)
        events = recent(store)
        events.append({"ts": ts, "kind": kind, "detail": detail})
        store.set_setting(_KEY, json.dumps(events[-_CAP:]))
    except Exception:
        pass


def recent(store) -> list[dict]:
    """The recorded events, oldest→newest; [] on absent/corrupt."""
    try:
        return json.loads(store.get_setting(_KEY) or "[]")
    except (ValueError, TypeError):
        return []
