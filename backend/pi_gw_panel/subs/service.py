import datetime
import logging
import threading
import uuid as _uuid

from pi_gw_panel.config import SETTINGS_DEFAULTS
from pi_gw_panel.controller import ApplyResult, apply_lock, apply_node
from pi_gw_panel.models import Subscription
from pi_gw_panel.subs.fetcher import fetch
from pi_gw_panel.subs.inject import host_tokens
from pi_gw_panel.subs.parsers.dispatch import parse_subscription
from pi_gw_panel.subs.reconcile import reconcile, security_kept

log = logging.getLogger("pi_gw_panel")

MAX_NODES = 500
_LOCKS_GUARD = threading.Lock()
_REFRESH_LOCKS: dict[int, threading.Lock] = {}


class FeedDowngradeRefused(RuntimeError):
    """The feed re-advertised the active node with weaker transport security, and reconcile
    refused to write that row. Carries the counts of the rest of the merge — which *did* land,
    because the refusal is scoped to the one row it protects — so the failed refresh can still
    report honestly what it changed."""

    def __init__(self, message: str, counts: dict):
        super().__init__(message)
        self.counts = counts


def _refresh_lock(sub_id: int) -> threading.Lock:
    with _LOCKS_GUARD:
        return _REFRESH_LOCKS.setdefault(sub_id, threading.Lock())


def prune_refresh_locks(known_ids) -> None:
    """Drop refresh locks for subscriptions that no longer exist (a held lock is left alone —
    its refresh is still running and will simply outlive one prune pass)."""
    with _LOCKS_GUARD:
        for sub_id in [key for key in _REFRESH_LOCKS if key not in known_ids]:
            if not _REFRESH_LOCKS[sub_id].locked():
                del _REFRESH_LOCKS[sub_id]


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
            # Reconcile already refused to write the weakened row, so there is nothing to
            # restore and no window in which the downgrade was visible. All that is left is to
            # fail the refresh loudly so the subscription goes red instead of looking healthy.
            if counts.get("active_downgrade"):
                stored, offered = counts["active_downgrade"]
                raise FeedDowngradeRefused(
                    "refusing the feed's security downgrade of the active node "
                    f"({stored} -> {offered}); the stored node was left unchanged", counts)
            applied = _restart_active(state, active_id, counts, old_active)
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
            # A refused downgrade fails the refresh but keeps the rest of the merge, so report
            # the counts it did apply rather than a flat zero that hides them.
            partial = getattr(exc, "counts", None) or {}
            result = {"ok": False, "status": current.last_status, "error": str(exc),
                      "added": partial.get("added", 0), "updated": partial.get("updated", 0),
                      "removed": partial.get("removed", 0), "path": path}
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


def _apply_userinfo(sub: Subscription, headers) -> None:
    """Parse the de-facto ``Subscription-Userinfo`` quota/expiry response header. Takes either
    a mapping or the raw ``[(name, value), …]`` list, and reads *every* occurrence — a provider
    that splits the fields across repeated headers used to have all but one silently dropped."""
    items = headers.items() if hasattr(headers, "items") else (headers or ())
    values: dict[str, int] = {}
    for header, raw in items:
        if str(header).lower() != "subscription-userinfo":
            continue
        for part in str(raw).replace(",", ";").split(";"):
            key, _, value = part.strip().partition("=")
            key = key.strip().lower()
            try:
                number = int(value.strip())
            except (ValueError, AttributeError):
                continue
            maximum = _MAX_EPOCH if key == "expire" else _MAX_BYTES
            if 0 <= number <= maximum:
                values[key] = number
    if not values:
        return
    sub.up_bytes = values.get("upload", sub.up_bytes)
    sub.down_bytes = values.get("download", sub.down_bytes)
    sub.total_bytes = values.get("total", sub.total_bytes)
    sub.expire_at = values.get("expire", sub.expire_at)


def _auto_switch_allowed(state, active_id, replacement_id) -> bool:
    """Gate the unattended "the active node vanished, take this one instead" switch.

    Everything about the replacement — address, uuid, reality keys — comes from remote feed
    content, and a scheduled refresh applies it with nobody watching. So it is operator-gated
    (``subs_auto_switch``), and it never accepts a node whose security is weaker than the one
    it replaces.
    """
    enabled = (state.store.get_setting("subs_auto_switch")
               or SETTINGS_DEFAULTS["subs_auto_switch"]) == "1"
    if not enabled:
        log.warning("subs: auto-switch to replacement node %s is disabled by setting; "
                    "the active node stays put", replacement_id)
        return False
    old = state.store.get_node(active_id) if active_id is not None else None
    new = state.store.get_node(replacement_id)
    if old is None or new is None:
        return new is not None
    if not security_kept(old, new):
        log.warning("subs: refusing to auto-switch the active node from security=%s to the "
                    "weaker security=%s offered by node %s", old.security, new.security,
                    replacement_id)
        return False
    return True


def _restart_active(state, active_id, counts, old_active=None):
    """Apply the refreshed active config and return its checked ApplyResult, if any.

    `active_id` was read inside the reconcile transaction, which has since committed. Between
    that read and this apply, auto-failover (a different thread) can move the tunnel to another
    node — and re-applying the node we captured would then silently revert a failover the panel
    performed for a reason. So take `apply_lock`, re-read the active node under it, and stand
    down if it moved (the same guard `health.failover.run` uses around its own preflight).

    `old_active` is the pre-reconcile snapshot of the active node, and the security ladder is
    enforced against it here too. Reconcile now refuses to write a weakened active row in the
    first place, so this is a backstop rather than the primary gate: it still catches a row
    that something *else* weakened between the merge and this apply, and it is what keeps the
    ladder covering the whole apply path and not just the merge."""
    replacement = counts.get("active_replacement")
    with apply_lock:
        current = state.store.get_setting("active_node_id")
        try:
            current_id = int(current) if current else None
        except (TypeError, ValueError):
            current_id = None
        if current_id != active_id:
            log.warning("subs: the active node moved to %s during refresh (was %s) — leaving "
                        "the live tunnel alone instead of reverting it", current_id, active_id)
            return None
        if replacement is not None and _auto_switch_allowed(state, active_id, replacement):
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
                # Backstop for a weakened row that did not come from this merge (reconcile
                # refuses those before they are written). Refuse loudly: the caller restores
                # the pre-refresh row and marks the refresh failed, which leaves both the
                # running tunnel and the stored config on the stronger setting instead of
                # quietly parking a downgraded config for the next Connect to pick up.
                if not security_kept(old_active, node):
                    log.warning("subs: refusing a feed-driven in-place downgrade of the active "
                                "node %s from security=%s to security=%s", active_id,
                                old_active.security, node.security)
                    return ApplyResult(
                        ok=False,
                        error=("refusing a feed-driven security downgrade of the active node: "
                               f"{old_active.security} -> {node.security}"))
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
