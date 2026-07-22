import datetime
import logging
import threading
import uuid as _uuid

from pi_gw_panel.controller import apply_node
from pi_gw_panel.models import Subscription
from pi_gw_panel.subs.fetcher import fetch
from pi_gw_panel.subs.inject import host_tokens
from pi_gw_panel.subs.parsers.dispatch import parse_subscription
from pi_gw_panel.subs.reconcile import reconcile

log = logging.getLogger("pi_gw_panel")

MAX_NODES = 500
_LOCKS_GUARD = threading.Lock()
_REFRESH_LOCKS: dict[int, threading.Lock] = {}


def _refresh_lock(sub_id: int) -> threading.Lock:
    with _LOCKS_GUARD:
        return _REFRESH_LOCKS.setdefault(sub_id, threading.Lock())


def refresh(state, sub: Subscription) -> dict:
    """Fetch → bounded parse → atomic reconcile; errors are persisted and returned, not raised."""
    if sub.id is None:
        return {"ok": False, "status": "error: subscription is not persisted",
                "error": "subscription is not persisted", "added": 0, "updated": 0,
                "removed": 0, "path": "direct"}
    with _refresh_lock(sub.id):
        # A queued manual/scheduler refresh must not reuse editable fields captured before a PATCH.
        current = state.store.get_subscription(sub.id)
        if current is None:
            return {"ok": False, "status": "error: subscription was deleted",
                    "error": "subscription was deleted", "added": 0, "updated": 0,
                    "removed": 0, "path": "direct"}
        proxy = tunnel_proxy(state)
        path = "tunnel" if proxy else "direct"
        success = False
        try:
            fetched_url = current.url
            fetched_injection = current.injection
            tokens = host_tokens(
                machine_id(), app_secret=getattr(state.settings, "session_secret", ""),
                subscription_id=current.id)
            body, path, headers = fetch(fetched_url, fetched_injection, tokens, proxy=proxy)
            parsed = parse_subscription(body, limit=MAX_NODES + 1)
            if not parsed:
                raise ValueError("zero valid nodes in subscription response")
            capped = len(parsed) > MAX_NODES
            parsed = parsed[:MAX_NODES]
            with state.store.transaction():
                latest = state.store.get_subscription(current.id)
                if latest is None:
                    raise RuntimeError("subscription was deleted during refresh")
                current = latest
                if current.url != fetched_url or current.injection != fetched_injection:
                    raise RuntimeError("subscription changed during fetch; refresh again")
                active = state.store.get_setting("active_node_id")
                try:
                    active_id = int(active) if active else None
                except (TypeError, ValueError):
                    active_id = None
                old_active = state.store.get_node(active_id) if active_id is not None else None
                counts = reconcile(
                    state.store, current.id, parsed, active_id, current.default_profile_id)
            applied = _restart_active(state, active_id, counts)
            if applied is not None and not getattr(applied, "ok", True):
                if old_active is not None and old_active.subscription_id == current.id:
                    state.store.update_node(old_active)
                raise RuntimeError(getattr(applied, "error", "active node re-apply failed"))
            _apply_userinfo(current, headers)
            note = f" (capped at {MAX_NODES})" if capped else ""
            skipped = counts.get("skipped_deletes", 0)
            if skipped:
                note += f" ({skipped} stale pending confirmation)"
            current.last_status = (
                f"ok: +{counts['added']} ~{counts['updated']} -{counts['removed']}{note}")
            current.last_path = path
            current.last_error = None
            current.last_fetched = _now_iso()
            success = True
            result = {**counts, "ok": True, "status": current.last_status, "error": None,
                      "path": path, "capped": capped}
        except Exception as exc:
            current.last_path = path
            current.last_status = f"error: {_short(exc)}"
            current.last_error = f"{type(exc).__name__}: {exc}"
            result = {"ok": False, "status": current.last_status, "error": str(exc),
                      "added": 0, "updated": 0, "removed": 0, "path": path}
        try:
            state.store.update_subscription_refresh(current, success=success)
        except Exception as exc:
            log.exception("subs.refresh: failed to persist lifecycle for subscription %s", current.id)
            result.update(ok=False, status=f"error: {_short(exc)}", error=str(exc))
        return result


def _short(exc: Exception, limit: int = 120) -> str:
    s = " ".join(str(exc).split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


_MAX_BYTES = 1 << 60
_MAX_EPOCH = 4102444800


def _apply_userinfo(sub: Subscription, headers: dict) -> None:
    """Parse the de-facto ``Subscription-Userinfo`` quota/expiry response header."""
    raw = None
    for key, value in (headers or {}).items():
        if key.lower() == "subscription-userinfo":
            raw = value
            break
    if not raw:
        return
    values: dict[str, int] = {}
    for part in raw.replace(",", ";").split(";"):
        key, _, value = part.strip().partition("=")
        key = key.strip().lower()
        try:
            number = int(value.strip())
        except (ValueError, AttributeError):
            continue
        maximum = _MAX_EPOCH if key == "expire" else _MAX_BYTES
        if 0 <= number <= maximum:
            values[key] = number
    sub.up_bytes = values.get("upload", sub.up_bytes)
    sub.down_bytes = values.get("download", sub.down_bytes)
    sub.total_bytes = values.get("total", sub.total_bytes)
    sub.expire_at = values.get("expire", sub.expire_at)


def _restart_active(state, active_id, counts):
    """Apply the refreshed active config and return its checked ApplyResult, if any."""
    replacement = counts.get("active_replacement")
    if replacement is not None:
        node = state.store.get_node(replacement)
        if node is not None:
            result = apply_node(node, state.settings, state.supervisor, state.net,
                                store=state.store, xray_bin=state.xray_bin)
            if (result is None or getattr(result, "ok", True)) and active_id is not None:
                state.store.delete_node(active_id)
            return result
    if counts.get("active_changed") and active_id is not None:
        node = state.store.get_node(active_id)
        if node is not None:
            return apply_node(node, state.settings, state.supervisor, state.net,
                              store=state.store, xray_bin=state.xray_bin)
    return None


def tunnel_proxy(state) -> str | None:
    if state.store.get_setting("tunneled_fetch") == "0":
        return None
    if not state.supervisor.status().get("running"):
        return None
    if not state.store.get_setting("active_node_id"):
        return None
    return f"http://127.0.0.1:{getattr(state.settings, 'local_proxy_port', 10808)}"


def machine_id() -> str:
    try:
        with open("/etc/machine-id") as stream:
            return stream.read().strip() or "unknown"
    except OSError:
        return f"{_uuid.getnode():012x}"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
