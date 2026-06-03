import datetime
import uuid as _uuid
from pi_gw_panel.models import Subscription
from pi_gw_panel.subs.inject import host_tokens
from pi_gw_panel.subs.fetcher import fetch
from pi_gw_panel.subs.parsers.dispatch import parse_subscription
from pi_gw_panel.subs.reconcile import reconcile


def refresh(state, sub: Subscription) -> dict:
    """Fetch (tunnel if available else direct) → parse → reconcile; update + persist sub.last_*.
    Single shared path for the manual refresh route and the scheduler. Never raises — failures
    are recorded in sub.last_status."""
    tokens = host_tokens(machine_id())
    proxy = tunnel_proxy(state)
    try:
        body, path = fetch(sub.url, sub.injection, tokens, proxy=proxy)
        parsed = parse_subscription(body)
        active = state.store.get_setting("active_node_id")
        active_id = int(active) if active is not None else None
        counts = reconcile(state.store, sub.id, parsed, active_id)
        sub.last_status = f"ok: +{counts['added']} ~{counts['updated']} -{counts['removed']}"
        sub.last_path = path
        result = {**counts, "path": path}
    except Exception as exc:  # network/parse failure → record, don't crash the loop
        sub.last_path = "tunnel" if proxy else "direct"
        sub.last_status = f"error: {exc}"
        result = {"added": 0, "updated": 0, "removed": 0, "error": str(exc), "path": sub.last_path}
    sub.last_fetched = _now_iso()
    state.store.update_subscription(sub)
    return result


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
