"""Backend coverage for the 2026-06-05 Dashboard audit (v1.10.3).

U2 — /status exposes prev_active_node_id (rollback target); apply_node records it.
NF4 — apply_node snapshots the data-used baseline; the WS frame's `session` = lifetime − baseline.
"""
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.models import Node
from pi_gw_panel.xray_supervisor.supervisor import XraySupervisor
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.controller import apply_node
from pi_gw_panel.app import _traffic_frame


def _node(store, name, uuid):
    return store.add_node(Node(id=None, name=name, address="1.2.3.4", port=443, uuid=uuid,
                               sni="s", public_key="PK", short_id="ab"))


def test_apply_node_snapshots_session_baseline_and_prev(settings, stub_xray):
    conn = connect(settings.db_path)
    init_schema(conn)
    store = NodeStore(conn)
    n1, n2 = _node(store, "n1", "u1"), _node(store, "n2", "u2")
    store.set_setting("data_used_up", "1000")
    store.set_setting("data_used_down", "5000")
    sup = XraySupervisor(xray_bin=stub_xray, config_path=settings.config_path)
    net = DryRunBackend()
    try:
        apply_node(store.get_node(n1), settings, sup, net, store=store, xray_bin=stub_xray)
        assert store.get_setting("session_base_up") == "1000"      # NF4: baseline = lifetime at connect
        assert store.get_setting("session_base_down") == "5000"
        apply_node(store.get_node(n2), settings, sup, net, store=store, xray_bin=stub_xray)
        assert store.get_setting("prev_active_node_id") == str(n1)  # U2: rollback target
    finally:
        sup.stop()


class _Sampler:
    totals = {"proxy": {"up": 0, "down": 0}}

    def sample(self):
        return {"proxy": {"up_bps": 0, "down_bps": 0}}


class _FrameState:
    def __init__(self, store):
        self.sampler = _Sampler()
        self.store = store


def test_traffic_frame_session_is_lifetime_minus_baseline():
    conn = connect(":memory:")
    init_schema(conn)
    store = NodeStore(conn)
    store.set_setting("data_used_up", "1000")
    store.set_setting("data_used_down", "5000")
    store.set_setting("session_base_up", "200")
    store.set_setting("session_base_down", "1000")
    f = _traffic_frame(_FrameState(store))
    assert f["lifetime"] == {"up": 1000, "down": 5000}
    assert f["session"] == {"up": 800, "down": 4000}      # NF4


def test_traffic_frame_session_defaults_to_lifetime_when_no_baseline():
    conn = connect(":memory:")
    init_schema(conn)
    store = NodeStore(conn)
    store.set_setting("data_used_up", "300")
    store.set_setting("data_used_down", "700")
    f = _traffic_frame(_FrameState(store))
    assert f["session"] == {"up": 300, "down": 700}       # no baseline → session == lifetime
