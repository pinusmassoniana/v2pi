"""Single source of truth for the active node's last health snapshot — real-request
result, egress IP, and freshness (`checked_at`). Shared by the live traffic WS frame and
the Network status panel so they never disagree (audit F3). Returns None when no node is
active or the active node has no recorded health yet."""
from pi_gw_panel.health import geo


def active_health(store) -> dict | None:
    aid = store.get_setting("active_node_id")
    if not aid:
        return None
    try:
        h = store.get_health(int(aid))
    except (TypeError, ValueError):
        return None
    if h is None:
        return None
    return {
        "node_id": h.node_id,
        "real_ok": h.last_real_ok,
        "latency_ms": h.last_real_ms,
        "egress_ip": h.egress_ip,
        "egress_ip6": h.egress_ip6,
        "egress_cc": geo.country_code(h.egress_ip),     # country flag next to the egress (v4)
        "egress_cc6": geo.country_code(h.egress_ip6),   # and v6
        "checked_at": h.checked_at,
        "lat_history": list(h.lat_history or []),   # recent latencies → dashboard sparkline (B)
    }
