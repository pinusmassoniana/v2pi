"""Coverage for the subscription-panel audit fixes/features (C1–C4, R2/R4, N5/N7/N9, scheduler)."""
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.models import Node, Subscription, TuningProfile, NodeHealth
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.subs.parsers.dispatch import parse_subscription
from pi_gw_panel.subs.reconcile import reconcile
from pi_gw_panel.subs.scheduler import SubScheduler
from pi_gw_panel.subs.service import _apply_userinfo


def _store(settings):
    conn = connect(settings.db_path, check_same_thread=False)
    init_schema(conn)
    return NodeStore(conn)


# --- D1 / C3: Node.normalize single source of truth ---
def test_normalize_manual_xhttp_becomes_xhttp_tls():
    n = Node(id=None, name="x", address="a", port=443, uuid="u", transport="xhttp")
    assert n.network == "xhttp" and n.flow == "" and n.security == "tls"


def test_normalize_reality_without_key_downgrades_to_tls():
    n = Node(id=None, name="x", address="a", port=443, uuid="u",
             security="reality", public_key="")
    assert n.security == "tls"


def test_normalize_vision_with_key_keeps_reality():
    n = Node(id=None, name="x", address="a", port=443, uuid="u",
             transport="vision", security="reality", public_key="PK")
    assert n.network == "tcp" and n.security == "reality" and n.flow == "xtls-rprx-vision"


# --- C1: clash / json parsers carry network/security/path ---
def test_clash_xhttp_sets_network_and_path():
    body = ("proxies:\n"
            "  - name: x1\n    type: vless\n    server: 1.2.3.4\n    port: 443\n"
            "    uuid: u\n    network: xhttp\n    servername: ya.ru\n"
            "    xhttp-opts:\n      path: /dl\n      headers:\n        Host: cdn.example\n")
    n = parse_subscription(body)[0]
    assert n.transport == "xhttp" and n.network == "xhttp"
    assert n.path == "/dl" and n.host == "cdn.example" and n.security == "tls"


def test_json_explicit_xhttp_tls():
    body = ('[{"name":"j","address":"9.9.9.9","port":443,"uuid":"u",'
            '"network":"xhttp","security":"tls","path":"/p"}]')
    n = parse_subscription(body)[0]
    assert n.transport == "xhttp" and n.network == "xhttp" and n.security == "tls" and n.path == "/p"


# --- C2 / N5: reconcile preserves user profile, new node inherits sub default ---
def test_reconcile_preserves_profile_and_inherits_default(settings):
    s = _store(settings)
    p_user = s.add_profile(TuningProfile(id=None, name="user"))
    p_def = s.add_profile(TuningProfile(id=None, name="def"))
    sid = s.add_subscription(Subscription(id=None, name="x", url="u", default_profile_id=p_def))
    s.add_node(Node(id=None, name="A", address="1.1.1.1", port=443, uuid="ua",
                    subscription_id=sid, tuning_profile_id=p_user))
    parsed = [Node(id=None, name="A", address="1.1.1.1", port=443, uuid="ua"),
              Node(id=None, name="B", address="2.2.2.2", port=443, uuid="ub")]
    reconcile(s, sid, parsed, active_node_id=None, default_profile_id=p_def)
    by_addr = {n.address: n for n in s.list_nodes_for_sub(sid)}
    assert by_addr["1.1.1.1"].tuning_profile_id == p_user   # C2 kept
    assert by_addr["2.2.2.2"].tuning_profile_id == p_def    # N5 inherited


# --- R4: dedup identical parsed entries ---
def test_reconcile_dedupes_identical_parsed(settings):
    s = _store(settings)
    sid = s.add_subscription(Subscription(id=None, name="x", url="u"))
    def dup():
        return Node(id=None, name="A", address="1.1.1.1", port=443, uuid="ua")
    counts = reconcile(s, sid, [dup(), dup()], active_node_id=None)
    assert len(s.list_nodes_for_sub(sid)) == 1 and counts["added"] == 1


# --- R2: delete_node clears the health row ---
def test_delete_node_removes_health(settings):
    s = _store(settings)
    nid = s.add_node(Node(id=None, name="A", address="1.1.1.1", port=443, uuid="u"))
    s.upsert_health(NodeHealth(node_id=nid, last_tcp_ok=True))
    s.delete_node(nid)
    assert s.get_health(nid) is None


# --- N7: Subscription-Userinfo parsing ---
def test_apply_userinfo_parses_header():
    sub = Subscription(id=None, name="s", url="u")
    _apply_userinfo(sub, {"Subscription-Userinfo":
                          "upload=100; download=200; total=1000; expire=1700000000"})
    assert sub.up_bytes == 100 and sub.down_bytes == 200
    assert sub.total_bytes == 1000 and sub.expire_at == 1700000000


def test_apply_userinfo_ignores_missing_header():
    sub = Subscription(id=None, name="s", url="u")
    _apply_userinfo(sub, {"content-type": "text/plain"})
    assert sub.total_bytes is None


# --- N2 / R1: scheduler skips disabled, honors persisted last_fetched ---
def test_scheduler_skips_disabled(settings):
    s = _store(settings)
    s.add_subscription(Subscription(id=None, name="s", url="u", interval_sec=100, enabled=False))
    st = type("S", (), {"store": s})()
    assert SubScheduler(st).due_subs(now=10_000.0) == []


def test_scheduler_recent_fetch_not_due_on_first_tick(settings):
    import datetime
    s = _store(settings)
    recent = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    sid = s.add_subscription(Subscription(id=None, name="s", url="u", interval_sec=3600))
    sub = s.get_subscription(sid)
    sub.last_fetched = recent
    s.update_subscription(sub)
    st = type("S", (), {"store": s})()
    assert SubScheduler(st).due_subs(now=10_000.0) == []   # fetched <1h ago → not due yet


# --- N9: best-node scoring ---
def test_pick_best_node_prefers_real_over_tcp(settings):
    from pi_gw_panel.api.routes import _pick_best_node
    s = _store(settings)
    n_tcp = s.add_node(Node(id=None, name="tcp", address="1.1.1.1", port=443, uuid="a"))
    n_real = s.add_node(Node(id=None, name="real", address="2.2.2.2", port=443, uuid="b"))
    s.upsert_health(NodeHealth(node_id=n_tcp, last_tcp_ok=True, last_tcp_ms=5))
    s.upsert_health(NodeHealth(node_id=n_real, last_real_ok=True, last_real_ms=50))
    assert _pick_best_node(s, None).id == n_real


def test_pick_best_node_skips_stale_and_other_scopes(settings):
    from pi_gw_panel.api.routes import _pick_best_node
    s = _store(settings)
    sid = s.add_subscription(Subscription(id=None, name="x", url="u"))
    s.add_node(Node(id=None, name="stale", address="1.1.1.1", port=443, uuid="a",
                    subscription_id=sid, stale=True))
    keep = s.add_node(Node(id=None, name="ok", address="2.2.2.2", port=443, uuid="b",
                           subscription_id=sid))
    assert _pick_best_node(s, sid).id == keep
    assert _pick_best_node(s, None) is None   # nothing manual
