import json
import threading
from pi_gw_panel import backup as backup_mod
from pi_gw_panel import controller
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.models import Node, TuningProfile, RoutingRule
from pi_gw_panel.xray_supervisor.supervisor import XraySupervisor
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.net_control.plan import NetResult
from pi_gw_panel.state import build_state
from pi_gw_panel.controller import apply_node, apply_net, restore_backup


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
        assert cfg["routing"]["rules"][1]["inboundTag"] == ["api"]   # [0] = loopback guard
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
    assert net.applied and "tproxy ip to" not in net.applied[-1]  # fail-closed guard installed
    assert store.get_setting("active_node_id") is None  # not persisted on failure


class _FailingNet:
    def __init__(self, *, guard_ok=True):
        self.apply_calls = 0
        self.guard_calls = 0
        self.teardown_calls = 0
        self.guard_ok = guard_ok

    def apply_tproxy(self, plan):
        self.apply_calls += 1
        return NetResult(ok=False, error="nft apply denied")

    def apply_guard(self, plan):
        self.guard_calls += 1
        return NetResult(ok=self.guard_ok, error="guard denied" if not self.guard_ok else "")

    def teardown(self):
        self.teardown_calls += 1
        return NetResult(ok=True)


def test_apply_node_rejects_failed_netresult_and_keeps_guard(settings, stub_xray):
    store, nid, sup, _ = _wire(settings, stub_xray)
    store.set_setting("kill_switch_enabled", "1")
    net = _FailingNet()
    res = apply_node(store.get_node(nid), settings, sup, net,
                     store=store, xray_bin=stub_xray)
    assert res.ok is False and "nft apply denied" in res.error
    assert store.get_setting("active_node_id") is None
    assert net.guard_calls == 1
    assert net.teardown_calls == 0, "fail-closed recovery must never remove the guard"
    assert getattr(net, "enforcement_status") == "ok"
    assert getattr(net, "wan_blocked") is True


def test_apply_node_reports_failed_guard_recovery(settings, stub_xray):
    store, nid, sup, _ = _wire(settings, stub_xray)
    store.set_setting("kill_switch_enabled", "1")
    net = _FailingNet(guard_ok=False)
    res = apply_node(store.get_node(nid), settings, sup, net,
                     store=store, xray_bin=stub_xray)
    assert res.ok is False
    assert "guard denied" in res.error
    assert getattr(net, "enforcement_status") == "error"
    assert getattr(net, "wan_blocked") is None


def test_same_node_reapply_preserves_real_rollback_target(settings, stub_xray):
    store, first, sup, net = _wire(settings, stub_xray)
    second = store.add_node(Node(id=None, name="n2", address="5.6.7.8", port=443,
                                 uuid="u-2"))
    try:
        assert apply_node(store.get_node(first), settings, sup, net,
                          store=store, xray_bin=stub_xray).ok
        assert apply_node(store.get_node(second), settings, sup, net,
                          store=store, xray_bin=stub_xray).ok
        assert store.get_setting("prev_active_node_id") == str(first)
        assert apply_node(store.get_node(second), settings, sup, net,
                          store=store, xray_bin=stub_xray).ok
        assert store.get_setting("prev_active_node_id") == str(first)
    finally:
        sup.stop()


def test_an_irreversible_apply_publishes_no_undo_and_an_ordinary_one_still_does(settings,
                                                                                stub_xray):
    """Both halves of the same contract, pinned together so neither can drift into the other.

    A revocation reaches the live config through this function whenever a node is connected, and
    `apply()` files the config it REPLACES as the undo — so that revocation used to publish a
    promotable pre-revocation config and lean on a sweep afterwards to take it away. Irreversible
    mode never writes the pairing at all.

    It must also stay strictly opt-in: publishing the undo target is the FEATURE for Connect,
    boot reapply, a subscription refresh and the failover tick, and an apply that quietly stopped
    doing it would take the operator's undo away everywhere to fix one caller.
    """
    from pi_gw_panel.xray_config.validate import ConfigManager

    store, first, sup, net = _wire(settings, stub_xray)
    second = store.add_node(Node(id=None, name="n2", address="5.6.7.8", port=443, uuid="u-2"))
    mgr = ConfigManager(settings, xray_bin=stub_xray)
    try:
        assert apply_node(store.get_node(first), settings, sup, net,
                          store=store, xray_bin=stub_xray).ok
        assert apply_node(store.get_node(second), settings, sup, net,
                          store=store, xray_bin=stub_xray).ok
        assert mgr.rollback_available() is True, "an ordinary apply stopped publishing an undo"

        assert apply_node(store.get_node(first), settings, sup, net, store=store,
                          xray_bin=stub_xray, irreversible=True).ok
        assert mgr.rollback_available() is False, \
            "an irreversible apply published a promotable rollback target"
        assert mgr.rollback() is False
        with open(settings.config_path) as f:               # ...and the write itself landed
            assert json.load(f)["outbounds"][0]["settings"]["vnext"][0]["address"] == "1.2.3.4"

        # Self-healing, exactly as a failed snapshot is: the next ordinary apply pairs again,
        # so one revocation does not cost the operator the undo forever.
        assert apply_node(store.get_node(second), settings, sup, net,
                          store=store, xray_bin=stub_xray).ok
        assert mgr.rollback_available() is True
        assert mgr.rollback() is True
    finally:
        sup.stop()


def test_restore_snapshot_is_taken_while_holding_the_mutation_lock(settings, stub_xray, monkeypatch):
    """FIX-E-2: the pre-restore snapshot must be captured under `apply_lock`, in the same
    serialized operation as the restore itself. Snapshotting before the lock left a gap where a
    concurrent mutation could commit and then get erased by the restore below without ever
    appearing in the recovery copy — the safety net had a hole exactly when it was needed.

    Proven from a second thread: while restore_backup is inside write_pre_restore_snapshot, a
    non-blocking acquire of `apply_lock` from that other thread must fail (lock already held).
    On the pre-fix code the snapshot ran before `with apply_lock:`, so the same probe would have
    found the lock free at that point.
    """
    settings.xray_bin = stub_xray
    state = build_state(settings, net=DryRunBackend())
    document = backup_mod.export_state(state.store)

    at_snapshot = threading.Event()
    resume = threading.Event()
    real_snapshot = backup_mod.write_pre_restore_snapshot

    def paused_snapshot(*args, **kwargs):
        result = real_snapshot(*args, **kwargs)
        at_snapshot.set()
        assert resume.wait(timeout=5), "test main thread never released the snapshot probe"
        return result

    monkeypatch.setattr(backup_mod, "write_pre_restore_snapshot", paused_snapshot)

    outcome = {}

    def run():
        outcome["result"] = restore_backup(state, document)

    t = threading.Thread(target=run)
    t.start()
    try:
        assert at_snapshot.wait(timeout=5), "restore never reached the snapshot step"
        acquired = controller.apply_lock.acquire(blocking=False)
        if acquired:
            controller.apply_lock.release()
    finally:
        resume.set()
        t.join(timeout=10)
    assert acquired is False, "apply_lock was not held while the pre-restore snapshot was taken"
    assert outcome["result"].ok is True
    state.close()
