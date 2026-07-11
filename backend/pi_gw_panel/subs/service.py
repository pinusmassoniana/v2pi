import datetime
import logging
import uuid as _uuid
from pi_gw_panel.models import Subscription
from pi_gw_panel.subs.inject import host_tokens
from pi_gw_panel.subs.fetcher import fetch
from pi_gw_panel.subs.parsers.dispatch import parse_subscription
from pi_gw_panel.subs.reconcile import reconcile
from pi_gw_panel.controller import apply_node

log = logging.getLogger("pi_gw_panel")

MAX_NODES = 500   # S3: a single subscription can't balloon the DB / reconcile loop


def refresh(state, sub: Subscription) -> dict:
    """Fetch (tunnel if available else direct) → parse → reconcile; update + persist sub.last_*.
    Single shared path for the manual refresh route and the scheduler. Never raises — failures
    are recorded in sub.last_status / sub.last_error."""
    tokens = host_tokens(machine_id())
    proxy = tunnel_proxy(state)
    try:
        body, path, headers = fetch(sub.url, sub.injection, tokens, proxy=proxy)
        parsed = parse_subscription(body)
        capped = len(parsed) > MAX_NODES
        parsed = parsed[:MAX_NODES]
        active = state.store.get_setting("active_node_id")
        active_id = int(active) if active is not None else None
        counts = reconcile(state.store, sub.id, parsed, active_id, sub.default_profile_id)
        _restart_active(state, active_id, counts)   # reconnect on a rotated/updated active server
        _apply_userinfo(sub, headers)               # N7: quota/expiry, when the provider sends it
        note = f" (capped at {MAX_NODES})" if capped else ""
        sub.last_status = f"ok: +{counts['added']} ~{counts['updated']} -{counts['removed']}{note}"
        sub.last_path = path
        sub.last_error = None
        result = {**counts, "path": path, "capped": capped}
    except Exception as exc:  # network/parse failure → record, don't crash the loop
        sub.last_path = "tunnel" if proxy else "direct"
        sub.last_status = f"error: {_short(exc)}"
        sub.last_error = f"{type(exc).__name__}: {exc}"
        result = {"added": 0, "updated": 0, "removed": 0, "error": str(exc), "path": sub.last_path}
    # P1: the persist itself must be guarded too — a store error here would propagate out of
    # refresh → the scheduler loop (no guard upstream) and kill it, so no sub ever refreshes
    # again. Keep the "never raises" contract: log and swallow instead.
    try:
        sub.last_fetched = _now_iso()
        state.store.update_subscription(sub)
    except Exception:
        log.exception("subs.refresh: failed to persist subscription %s", sub.id)
    return result


def _short(exc: Exception, limit: int = 120) -> str:
    """A one-line status summary; the full text goes to sub.last_error (N6)."""
    s = " ".join(str(exc).split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


_MAX_BYTES = 1 << 60           # 1 EiB — beyond any real quota; reject absurd/overflow values
_MAX_EPOCH = 4102444800        # ~year 2100 — reject nonsensical far-future expiries


def _apply_userinfo(sub: Subscription, headers: dict) -> None:
    """Parse a `Subscription-Userinfo: upload=…; download=…; total=…; expire=…` header
    (the de-facto panel standard) onto the sub. Absent/garbled → leave fields untouched."""
    raw = None
    for k, v in (headers or {}).items():
        if k.lower() == "subscription-userinfo":
            raw = v
            break
    if not raw:
        return
    vals: dict[str, int] = {}
    for part in raw.replace(",", ";").split(";"):
        key, _, val = part.strip().partition("=")
        key = key.strip().lower()
        try:
            n = int(val.strip())
        except (ValueError, AttributeError):
            continue
        # N7 hardening: the numbers are attacker-controlled — ignore negatives and absurd
        # magnitudes rather than storing them unbounded (overflow / nonsense quota + expiry).
        hi = _MAX_EPOCH if key == "expire" else _MAX_BYTES
        if 0 <= n <= hi:
            vals[key] = n
    sub.up_bytes = vals.get("upload", sub.up_bytes)
    sub.down_bytes = vals.get("download", sub.down_bytes)
    sub.total_bytes = vals.get("total", sub.total_bytes)
    sub.expire_at = vals.get("expire", sub.expire_at)


def _restart_active(state, active_id, counts) -> None:
    """Re-apply the live tunnel after a refresh so it reconnects on the refreshed config:
    - config rotated in place (same identity, new reality key/sni) → reconnect on the same node;
    - the active identity vanished and exactly one fresh node remains (`active_replacement`) →
      move the connection to it and drop the now-stale old node.
    apply_node never raises.

    DEFERRED HARDENING (audit BF S7): the replacement path trusts feed content — a hostile provider
    could drop the real server and serve one attacker node to redirect live traffic. The proper fix
    is a per-subscription 'auto-reconnect on identity rotation' opt-in (needs a Subscription schema
    field + a UI toggle); until then this preserves the project's zero-touch recovery rather than
    silently disabling it."""
    rep = counts.get("active_replacement")
    if rep is not None:
        node = state.store.get_node(rep)
        if node is not None:
            res = apply_node(node, state.settings, state.supervisor, state.net,
                             store=state.store, xray_bin=state.xray_bin)
            if (res is None or getattr(res, "ok", True)) and active_id is not None:
                state.store.delete_node(active_id)   # drop the stale old identity after the switch
    elif counts.get("active_changed") and active_id is not None:   # config rotated in place
        node = state.store.get_node(active_id)
        if node is not None:
            apply_node(node, state.settings, state.supervisor, state.net,
                       store=state.store, xray_bin=state.xray_bin)


def tunnel_proxy(state) -> str | None:
    """Local proxy URL iff tunneled-fetch is enabled AND xray is running AND a node is active."""
    if state.store.get_setting("tunneled_fetch") == "0":
        return None
    if not state.supervisor.status().get("running"):
        return None
    if state.store.get_setting("active_node_id") is None:
        return None
    port = getattr(state.settings, "local_proxy_port", 10808)
    return f"http://127.0.0.1:{port}"


def machine_id() -> str:
    try:
        with open("/etc/machine-id") as f:
            return f.read().strip() or "unknown"
    except OSError:
        return f"{_uuid.getnode():012x}"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
