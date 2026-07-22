from datetime import datetime, timezone

from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.health.snapshot import health_status
from pi_gw_panel.models import Node, NodeHealth
from pi_gw_panel.nodes.store import NodeStore


def _ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def test_health_status_exposes_explicit_freshness_and_eligible_standbys():
    conn = connect(":memory:")
    init_schema(conn)
    store = NodeStore(conn)
    active = store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u1"))
    fresh = store.add_node(Node(id=None, name="b", address="2.2.2.2", port=443, uuid="u2"))
    stale = store.add_node(Node(id=None, name="c", address="3.3.3.3", port=443, uuid="u3"))
    store.set_setting("active_node_id", str(active))
    store.upsert_health(NodeHealth(node_id=active, last_real_ok=True, checked_at=_ts(970)))
    store.upsert_health(NodeHealth(node_id=fresh, last_tcp_ok=True, checked_at=_ts(950)))
    store.upsert_health(NodeHealth(node_id=stale, last_http_ok=True, checked_at=_ts(900)))

    assert health_status(store, now=1000, freshness_ttl=60) == {
        "active_health_fresh": True,
        "active_health_age_sec": 30.0,
        "health_freshness_ttl_sec": 60,
        "eligible_standby_count": 1,
    }
