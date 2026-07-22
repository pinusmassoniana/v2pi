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


def test_nft_bypasses_dhcp_before_tproxy():
    # A DHCP DISCOVER is a broadcast to 255.255.255.255 — NOT covered by the RFC-1918
    # daddr return — so without an explicit carve-out it falls through to the tproxy
    # rule and xray steals it, leaving segment clients unable to get a lease. The
    # bypass must precede the catch-all tproxy line.
    lines = render_nft(_plan()).splitlines()
    dhcp_idx = next(i for i, l in enumerate(lines) if "udp dport { 67, 68 } return" in l)
    tproxy_idx = next(i for i, l in enumerate(lines) if "tproxy ip to" in l)
    assert dhcp_idx < tproxy_idx


def test_nft_tproxy_scoped_to_segment_iface():
    # tproxy must only catch traffic arriving on the segment iface — not the Docker
    # bridge or other host-forwarded traffic (which would otherwise get tunneled).
    line = next(l for l in render_nft(_plan()).splitlines() if "tproxy ip to" in l)
    assert 'iifname "eth0.2"' in line


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


def test_dnsmasq_has_leasefile():
    text = render_dnsmasq(_plan())
    assert any(l.startswith("dhcp-leasefile=") and l.endswith("dnsmasq.leases")
               for l in text.splitlines())


def test_dnsmasq_no_v6_block_when_disabled():
    text = render_dnsmasq(_plan())          # ipv6_enabled defaults False
    assert "enable-ra" not in text
    assert "constructor:" not in text


def test_dnsmasq_v6_block_when_enabled():
    p = NetPlan.from_settings(Settings())
    p.ipv6_enabled = True
    p.segment_ip6 = "fd00:1:2:3::/64"
    text = render_dnsmasq(p)
    assert "enable-ra" in text
    assert "dhcp-range=::,constructor:eth0.2,ra-stateless,64,12h" in text
    assert "dhcp-option=option6:dns-server,[2606:4700:4700::1111]" in text


def test_from_store_override_beats_config_and_resolves_killswitch():
    store = _store()
    # A fresh database is fail-closed by default (migration 15).
    p0 = NetPlan.from_store(store, Settings())
    assert p0.segment_iface == "eth0.2"
    assert p0.kill_switch is True
    store.set_setting("kill_switch_enabled", "0")
    assert NetPlan.from_store(store, Settings()).kill_switch is False
    # DB overrides win; kill-switch flag resolved from settings k/v
    store.set_setting("segment_iface", "eth0.9")
    store.set_setting("dhcp_end", "192.168.10.250")
    store.set_setting("kill_switch_enabled", "1")
    p1 = NetPlan.from_store(store, Settings())
    assert p1.segment_iface == "eth0.9"
    assert p1.dhcp_end == "192.168.10.250"
    assert p1.kill_switch is True
    assert p1.tproxy_port == 52345                   # system knobs stay config-only


# --- LAN access: segment → home-LAN masquerade (default on) ---
def test_nft_lan_access_on_masquerades_segment_to_lan():
    text = render_nft(_plan())                       # lan_access defaults on (config)
    assert "chain postrouting" in text
    assert "type nat hook postrouting" in text
    # scoped: segment /24 → home-LAN /24 out the mgmt iface only — never the WAN
    assert "ip saddr 192.168.10.0/24 ip daddr 192.168.1.0/24" in text
    assert 'oifname "eth0" masquerade' in text


def test_nft_lan_access_off_has_no_masquerade():
    p = NetPlan.from_settings(Settings())
    p.lan_access = False
    text = render_nft(p)
    assert "masquerade" not in text
    assert "chain postrouting" not in text
    assert "tproxy ip to :52345" in text             # tproxy path still intact


def test_nft_lan_access_masquerade_independent_of_tunnel_state():
    # Reaching the router/host needs no tunnel, so the masquerade must render even tunnel-down.
    assert "masquerade" in render_nft(_plan(), tunnel_up=False)
