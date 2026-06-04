from pi_gw_panel.config import Settings
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.state import AppState
from pi_gw_panel.models import Node
from pi_gw_panel.controller import reapply_active_node, ApplyResult


def _state() -> AppState:
    conn = connect(":memory:")
    init_schema(conn)
    return AppState(settings=Settings(), store=NodeStore(conn),
                    supervisor=None, net=None, xray_bin="xray")


def test_no_active_node_is_noop():
    assert reapply_active_node(_state()) is None


def test_reapplies_saved_active_node(monkeypatch):
    st = _state()
    nid = st.store.add_node(Node(id=None, name="n", address="1.2.3.4", port=443, uuid="u"))
    st.store.set_setting("active_node_id", str(nid))
    seen = []
    monkeypatch.setattr("pi_gw_panel.controller.apply_node",
                        lambda node, *a, **k: (seen.append(node), ApplyResult(ok=True))[1])
    res = reapply_active_node(st)
    assert res.ok and [n.id for n in seen] == [nid]


def test_missing_saved_node_is_safe(monkeypatch):
    st = _state()
    st.store.set_setting("active_node_id", "999")          # points at a deleted node
    called = []
    monkeypatch.setattr("pi_gw_panel.controller.apply_node",
                        lambda *a, **k: (called.append(1), ApplyResult(ok=True))[1])
    assert reapply_active_node(st) is None                  # no crash, no apply attempt
    assert called == []
