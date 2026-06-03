from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.models import Node, TuningProfile, RoutingRule, NodeHealth


def _store(settings) -> NodeStore:
    conn = connect(settings.db_path)
    init_schema(conn)
    return NodeStore(conn)


def test_tuning_profile_crud_and_default(settings):
    s = _store(settings)
    # migration 2 seeded a 'default' profile and stored its id
    seeded = s.get_default_profile()
    assert seeded is not None and seeded.name == "default"

    pid = s.add_profile(TuningProfile(id=None, name="frag-on", frag_enabled=True, quic="drop"))
    got = s.get_profile(pid)
    assert got.name == "frag-on" and got.frag_enabled is True and got.quic == "drop"
    assert got.doh_enabled is True              # dataclass default carried through
    assert {p.name for p in s.list_profiles()} >= {"default", "frag-on"}

    got.name = "frag-renamed"
    got.mux_enabled = True
    s.update_profile(got)
    again = s.get_profile(pid)
    assert again.name == "frag-renamed" and again.mux_enabled is True

    s.set_default_profile(pid)
    assert s.get_default_profile().id == pid

    s.delete_profile(pid)
    assert s.get_profile(pid) is None


def test_delete_profile_detaches_nodes(settings):
    s = _store(settings)
    pid = s.add_profile(TuningProfile(id=None, name="p"))
    nid = s.add_node(Node(id=None, name="n", address="a", port=1, uuid="u", tuning_profile_id=pid))
    s.delete_profile(pid)
    assert s.get_node(nid).tuning_profile_id is None


def test_routing_rules_replace_and_get(settings):
    s = _store(settings)
    assert s.get_routing() == []
    s.replace_routing([
        RoutingRule(id=None, position=0, type="geoip", value="ru", action="direct"),
        RoutingRule(id=None, position=1, type="domain", value="example.com", action="block"),
    ])
    rules = s.get_routing()
    assert [r.type for r in rules] == ["geoip", "domain"]
    assert [r.position for r in rules] == [0, 1]
    assert rules[1].action == "block" and rules[0].value == "ru"

    # replace overwrites entirely and re-positions from zero
    s.replace_routing([RoutingRule(id=None, position=9, type="port", value="443", action="proxy")])
    rules = s.get_routing()
    assert len(rules) == 1 and rules[0].type == "port" and rules[0].position == 0


def test_node_health_upsert_get_list(settings):
    s = _store(settings)
    nid = s.add_node(Node(id=None, name="n", address="a", port=1, uuid="u"))
    assert s.get_health(nid) is None

    s.upsert_health(NodeHealth(node_id=nid, last_tcp_ok=True, last_tcp_ms=12,
                               last_real_ok=False, last_real_ms=None, egress_ip="1.2.3.4",
                               checked_at="2026-06-03T00:00:00Z", fail_count=2))
    h = s.get_health(nid)
    assert h.last_tcp_ok is True and h.last_tcp_ms == 12
    assert h.last_real_ok is False and h.egress_ip == "1.2.3.4"
    assert h.fail_count == 2

    # upsert again on the same node updates in place (PK conflict)
    s.upsert_health(NodeHealth(node_id=nid, last_tcp_ok=True, last_real_ok=True, fail_count=0))
    h = s.get_health(nid)
    assert h.last_real_ok is True and h.fail_count == 0
    assert [x.node_id for x in s.list_health()] == [nid]


def test_node_carries_tuning_profile_id(settings):
    s = _store(settings)
    pid = s.add_profile(TuningProfile(id=None, name="p"))
    nid = s.add_node(Node(id=None, name="n", address="a", port=1, uuid="u", tuning_profile_id=pid))
    assert s.get_node(nid).tuning_profile_id == pid
    n = s.get_node(nid)
    n.tuning_profile_id = None
    s.update_node(n)
    assert s.get_node(nid).tuning_profile_id is None
