import json
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.models import Node, TuningProfile, RoutingRule
from pi_gw_panel.xray_supervisor.supervisor import XraySupervisor
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.controller import apply_node, apply_net


def _wire(settings, stub_xray):
    conn = connect(settings.db_path)
    init_schema(conn)
    store = NodeStore(conn)
    nid = store.add_node(Node(id=None, name="n1", address="1.2.3.4", port=47000,
                              uuid="u-1", sni="www.microsoft.com",
                              public_key="PK", short_id="ab12"))
    sup = XraySupervisor(xray_bin=stub_xray, config_path=settings.config_path)
    net = DryRunBackend()
    return store, nid, sup, net


def test_apply_node_success_path(settings, stub_xray):
    store, nid, sup, net = _wire(settings, stub_xray)
    try:
        res = apply_node(store.get_node(nid), settings, sup, net, xray_bin=stub_xray)
        assert res.ok is True
        with open(settings.config_path) as f:
            cfg = json.load(f)
        assert cfg["outbounds"][0]["settings"]["vnext"][0]["address"] == "1.2.3.4"
        # (first apply has no previous config to snapshot — undo-target behavior is
        #  covered in test_validate.py::test_apply_snapshots_previous_config_for_undo)
        assert sup.status()["running"] is True                    # xray reloaded
        assert len(net.applied) == 1                              # net ruleset applied
    finally:
        sup.stop()


def test_apply_node_validation_failure_is_safe(settings, stub_xray, monkeypatch):
    store, nid, sup, net = _wire(settings, stub_xray)
    monkeypatch.setenv("STUB_XRAY_FAIL", "1")
    res = apply_node(store.get_node(nid), settings, sup, net, xray_bin=stub_xray)
    assert res.ok is False
    assert "error" in res.error.lower()
    assert sup.status()["running"] is False        # xray NOT started on bad config
    assert net.applied == []                        # net NOT touched on bad config


def test_apply_node_persists_active_node(settings, stub_xray):
    store, nid, sup, net = _wire(settings, stub_xray)
    try:
        res = apply_node(store.get_node(nid), settings, sup, net,
                         store=store, xray_bin=stub_xray)
        assert res.ok is True
        assert store.get_setting("active_node_id") == str(nid)
    finally:
        sup.stop()


def test_apply_node_reflects_profile_and_routing_from_store(settings, stub_xray):
    store, nid, sup, net = _wire(settings, stub_xray)
    # assign a custom profile (frag on, quic drop), a routing rule, and a default action
    pid = store.add_profile(TuningProfile(id=None, name="custom", frag_enabled=True, quic="drop"))
    n = store.get_node(nid)
    n.tuning_profile_id = pid
    store.update_node(n)
    store.replace_routing([RoutingRule(id=None, position=0, type="domain",
                                       value="x.com", action="block")])
    store.set_setting("routing_default_action", "direct")
    try:
        res = apply_node(store.get_node(nid), settings, sup, net, store=store, xray_bin=stub_xray)
        assert res.ok is True
        with open(settings.config_path) as f:
            cfg = json.load(f)
        assert any(o["tag"] == "fragment" for o in cfg["outbounds"])         # profile frag
        rules = cfg["routing"]["rules"]
        assert {"type": "field", "domain": ["x.com"], "outboundTag": "block"} in rules
        assert rules[-1]["outboundTag"] == "direct"                          # custom default action
        assert any(r.get("protocol") == ["quic"] and r["outboundTag"] == "block" for r in rules)
    finally:
        sup.stop()


def test_apply_node_includes_stats_when_enabled(settings, stub_xray):
    store, nid, sup, net = _wire(settings, stub_xray)
    try:
        res = apply_node(store.get_node(nid), settings, sup, net, store=store, xray_bin=stub_xray)
        assert res.ok is True
        with open(settings.config_path) as f:
            cfg = json.load(f)
        assert cfg.get("stats") == {}                                   # default stats_enabled=1
        assert any(i["tag"] == "api" for i in cfg["inbounds"])
        assert cfg["routing"]["rules"][0]["inboundTag"] == ["api"]
    finally:
        sup.stop()


def test_apply_node_omits_stats_when_disabled(settings, stub_xray):
    store, nid, sup, net = _wire(settings, stub_xray)
    store.set_setting("stats_enabled", "0")
    try:
        res = apply_node(store.get_node(nid), settings, sup, net, store=store, xray_bin=stub_xray)
        assert res.ok is True
        with open(settings.config_path) as f:
            cfg = json.load(f)
        assert "stats" not in cfg
        assert all(i["tag"] != "api" for i in cfg["inbounds"])
    finally:
        sup.stop()


def test_apply_net_builds_plan_from_store_overrides(settings, stub_xray):
    store, _, _, net = _wire(settings, stub_xray)
    store.set_setting("segment_iface", "eth0.9")
    store.set_setting("kill_switch_enabled", "1")
    res = apply_net(settings, net, store)
    assert res.ok is True
    assert len(net.applied) == 1
    assert "interface=eth0.9" in net.applied[0]       # dnsmasq reflects the edited iface
    assert 'iifname "eth0.9"' in net.applied[0]        # kill-switch drop on the new iface
    assert "chain forward" in net.applied[0]


def test_apply_net_without_store_uses_config_defaults(settings, stub_xray):
    _, _, _, net = _wire(settings, stub_xray)
    res = apply_net(settings, net, None)
    assert res.ok is True
    assert "interface=eth0.2" in net.applied[0]        # config default
    assert "chain forward" not in net.applied[0]       # kill-switch off by default


def test_apply_node_rolls_back_when_reload_fails(settings, stub_xray):
    store, nid, _, net = _wire(settings, stub_xray)
    bad_sup = XraySupervisor(xray_bin="/nonexistent/xray-bin",
                             config_path=settings.config_path)
    res = apply_node(store.get_node(nid), settings, bad_sup, net,
                     store=store, xray_bin=stub_xray)
    assert res.ok is False
    assert "apply failed" in res.error.lower()
    assert net.applied == []                            # net never applied / torn down
    assert store.get_setting("active_node_id") is None  # not persisted on failure
