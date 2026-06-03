from pi_gw_panel.config import Settings
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.net_control.plan import NetPlan
from pi_gw_panel.net_control.render import render_nft, render_dnsmasq


def _plan():
    return NetPlan.from_settings(Settings())


def _store():
    conn = connect(":memory:")
    init_schema(conn)
    return NodeStore(conn)


def test_nft_has_tproxy_mark_and_port():
    text = render_nft(_plan())
    assert "table ip pi_gw_panel" in text
    assert "tproxy ip to :52345" in text
    assert "meta mark set 0x40" in text
    assert "meta mark 0x80 return" in text        # anti-loop: skip xray egress
    assert "chain prerouting" in text


def test_nft_redirects_both_tcp_and_udp():
    text = render_nft(_plan())
    assert "meta l4proto { tcp, udp }" in text


def test_dnsmasq_serves_segment_dhcp():
    text = render_dnsmasq(_plan())
    assert "interface=eth0.2" in text
    assert "dhcp-range=192.168.10.30,192.168.10.200,12h" in text
    assert "dhcp-option=3,192.168.10.2" in text     # gateway = Pi segment
    assert "dhcp-option=6,1.1.1.1" in text          # client DNS (tproxy'd)
    assert "server=" not in text                    # not a forwarding resolver / no loop


# --- Wave 3b: kill-switch + store-resolved plan ---
def test_nft_killswitch_off_is_unchanged():
    text = render_nft(_plan())                       # kill_switch defaults False
    assert "chain forward" not in text               # no forward chain when off
    assert " drop" not in text
    assert "tproxy ip to :52345" in text             # prerouting tproxy path intact


def test_nft_killswitch_on_drops_segment_to_wan():
    plan = NetPlan.from_settings(Settings())
    plan.kill_switch = True
    text = render_nft(plan)
    assert "chain forward" in text
    assert 'iifname "eth0.2"' in text                # fail-closed on the client segment
    assert "ip daddr != { 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 } drop" in text
    assert "tproxy ip to :52345" in text             # prerouting chain still present


def test_from_store_override_beats_config_and_resolves_killswitch():
    store = _store()
    # absent overrides ⇒ config defaults, kill-switch off
    p0 = NetPlan.from_store(store, Settings())
    assert p0.segment_iface == "eth0.2"
    assert p0.kill_switch is False
    # DB overrides win; kill-switch flag resolved from settings k/v
    store.set_setting("segment_iface", "eth0.9")
    store.set_setting("dhcp_end", "192.168.10.250")
    store.set_setting("kill_switch_enabled", "1")
    p1 = NetPlan.from_store(store, Settings())
    assert p1.segment_iface == "eth0.9"
    assert p1.dhcp_end == "192.168.10.250"
    assert p1.kill_switch is True
    assert p1.tproxy_port == 52345                   # system knobs stay config-only
