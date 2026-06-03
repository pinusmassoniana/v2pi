from pi_gw_panel.config import Settings
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.models import NodeHealth
from pi_gw_panel.net_control import netcheck


def _store():
    conn = connect(":memory:")
    init_schema(conn)
    return NodeStore(conn)


def _sysfs_with(tmp_path, iface, value):
    d = tmp_path / iface
    d.mkdir(parents=True)
    (d / "operstate").write_text(value + "\n")
    return str(tmp_path)


def test_segment_up_true_when_operstate_up(tmp_path):
    assert netcheck.segment_up("eth0.2", sysfs=_sysfs_with(tmp_path, "eth0.2", "up")) is True


def test_segment_up_false_when_operstate_down(tmp_path):
    assert netcheck.segment_up("eth0.2", sysfs=_sysfs_with(tmp_path, "eth0.2", "down")) is False


def test_segment_up_none_when_sysfs_absent(tmp_path):
    assert netcheck.segment_up("eth0.2", sysfs=str(tmp_path)) is None


def test_dhcp_clients_counts_nonblank_lease_lines(tmp_path):
    leases = tmp_path / "dnsmasq.leases"
    leases.write_text("1718 aa:bb 192.168.10.30 host1 *\n\n1718 cc:dd 192.168.10.31 host2 *\n")
    assert netcheck.dhcp_clients(str(leases)) == 2


def test_dhcp_clients_zero_when_file_missing(tmp_path):
    assert netcheck.dhcp_clients(str(tmp_path / "nope.leases")) == 0


def test_network_status_shape_with_active_tunnel(tmp_path):
    store = _store()
    store.set_setting("active_node_id", "1")
    store.upsert_health(NodeHealth(node_id=1, last_real_ok=True, last_real_ms=42, egress_ip="9.9.9.9"))
    leases = tmp_path / "leases"
    leases.write_text("a\nb\nc\n")
    st = netcheck.network_status(store, Settings(),
                                 sysfs=_sysfs_with(tmp_path, "eth0.2", "up"), leases_path=str(leases))
    assert st["segment_up"] is True
    assert st["dhcp_clients"] == 3
    assert st["tunnel"] == {"real_ok": True, "latency_ms": 42, "egress_ip": "9.9.9.9"}


def test_network_status_defaults_are_graceful_in_dev():
    # No active node, default sysfs/leases (absent on macOS) → unknown/0, never raises.
    st = netcheck.network_status(_store(), Settings())
    assert st["segment_up"] is None
    assert st["dhcp_clients"] == 0
    assert st["tunnel"] == {"real_ok": None, "latency_ms": None, "egress_ip": None}


def test_router_recommendations_parse_vlan_and_guidance():
    recs = netcheck.router_recommendations(Settings())
    assert recs and all("title" in r and "detail" in r for r in recs)
    blob = " ".join(r["title"] + " " + r["detail"] for r in recs)
    assert "VLAN 2" in blob                                  # parsed from eth0.2
    assert "192.168.10.30" in blob and "192.168.10.200" in blob   # Pi serves this range
    assert "DHCP" in blob                                    # disable router DHCP on that VLAN
