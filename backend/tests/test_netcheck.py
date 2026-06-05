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


def test_dhcp_clients_counts_unexpired_leases(tmp_path):
    leases = tmp_path / "dnsmasq.leases"
    # col0 = expiry epoch (0 = no expiry); a far-past expiry is dropped (audit F4).
    leases.write_text(
        "0 aa:bb 192.168.10.30 host1 *\n\n"
        "9999999999 cc:dd 192.168.10.31 host2 *\n"
        "100 ee:ff 192.168.10.32 stale *\n")
    assert netcheck.dhcp_clients(str(leases)) == 2


def test_dhcp_leases_parse_fields_and_drop_expired(tmp_path):
    leases = tmp_path / "leases"
    leases.write_text(
        "0 aa:bb:cc:dd:ee:01 192.168.10.30 iPhone 01:aa\n"
        "9999999999 aa:bb:cc:dd:ee:02 192.168.10.31 * 01:bb\n"
        "100 aa:bb:cc:dd:ee:03 192.168.10.32 stale *\n")
    rows = netcheck.dhcp_leases(str(leases))
    assert [r["ip"] for r in rows] == ["192.168.10.30", "192.168.10.31"]
    assert rows[0]["hostname"] == "iPhone" and rows[1]["hostname"] == ""   # '*' → ""
    assert rows[0]["mac"] == "aa:bb:cc:dd:ee:01"


def test_dhcp_clients_zero_when_file_missing(tmp_path):
    assert netcheck.dhcp_clients(str(tmp_path / "nope.leases")) == 0


def test_network_status_shape_with_active_tunnel(tmp_path):
    store = _store()
    store.set_setting("active_node_id", "1")
    store.upsert_health(NodeHealth(node_id=1, last_real_ok=True, last_real_ms=42,
                                   egress_ip="9.9.9.9", checked_at="2026-06-04T00:00:00+00:00"))
    leases = tmp_path / "leases"
    leases.write_text("0 aa:bb 192.168.10.30 host1 *\n0 cc:dd 192.168.10.31 host2 *\n")
    st = netcheck.network_status(store, Settings(),
                                 sysfs=_sysfs_with(tmp_path, "eth0.2", "up"), leases_path=str(leases))
    assert st["segment_up"] is True
    assert st["dhcp_clients"] == 2
    assert [c["ip"] for c in st["clients"]] == ["192.168.10.30", "192.168.10.31"]
    assert st["tunnel"] == {"real_ok": True, "latency_ms": 42, "egress_ip": "9.9.9.9",
                            "checked_at": "2026-06-04T00:00:00+00:00"}


def test_network_status_defaults_are_graceful_in_dev():
    # No active node, default sysfs/leases (absent on macOS) → unknown/0, never raises.
    st = netcheck.network_status(_store(), Settings())
    assert st["segment_up"] is None
    assert st["dhcp_clients"] == 0
    assert st["clients"] == []
    assert st["tunnel"] == {"real_ok": None, "latency_ms": None, "egress_ip": None, "checked_at": None}


def test_router_recommendations_parse_vlan_and_guidance():
    recs = netcheck.router_recommendations(Settings())
    assert recs and all("title" in r and "detail" in r for r in recs)
    blob = " ".join(r["title"] + " " + r["detail"] for r in recs)
    assert "VLAN 2" in blob                                  # parsed from eth0.2
    assert "192.168.10.30" in blob and "192.168.10.200" in blob   # Pi serves this range
    assert "DHCP" in blob                                    # disable router DHCP on that VLAN


def test_router_recommendations_v6_includes_disable_router_ra():
    recs = netcheck.router_recommendations(Settings(), ipv6_enabled=True, segment_ip6="fd00:1:2:3::/64")
    blob = " ".join(r["title"] + " " + r["detail"] for r in recs)
    assert "Router Advertisement" in blob and "leak" in blob


def test_foreign_ra_detects_other_router():
    sample = ("fe80::1 lladdr aa:bb:cc:dd:ee:ff router REACHABLE\n"
              "fe80::2 lladdr 11:22:33:44:55:66 STALE\n")
    assert netcheck.foreign_ra("eth0.2", run=lambda cmd: sample) is True


def test_foreign_ra_false_when_no_router_neighbor():
    sample = "fe80::2 lladdr 11:22:33:44:55:66 STALE\n"
    assert netcheck.foreign_ra("eth0.2", run=lambda cmd: sample) is False


def test_foreign_ra_none_on_error():
    def boom(cmd):
        raise OSError("no ip")
    assert netcheck.foreign_ra("eth0.2", run=boom) is None
