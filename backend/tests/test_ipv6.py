"""IPv6 tunnel support (spec docs/2026-06-04-ipv6-support-design.md).

render_nft6 modes · LinuxBackend v6 tproxy + ip -6 policy routing · builder tproxy-in6 ·
NetPlan resolution · gated v6 recommendations · PUT /network toggle rebuilds the live config.
"""
import json
import subprocess
from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.config import Settings
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.models import Node
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.net_control.linux import LinuxBackend
from pi_gw_panel.net_control.plan import NetPlan
from pi_gw_panel.net_control.render import render_nft6
from pi_gw_panel.net_control import netcheck
from pi_gw_panel.xray_config.builder import build_config


def _store():
    conn = connect(":memory:")
    init_schema(conn)
    return NodeStore(conn)


def _plan(**kw):
    p = NetPlan.from_settings(Settings())
    for k, v in kw.items():
        setattr(p, k, v)
    return p


class FakeRun:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, input=None):
        self.calls.append((cmd, input))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def cmds(self):
        return [c for c, _ in self.calls]


# --- render_nft6 modes (the §4 matrix) -------------------------------------

def test_render_nft6_tproxy_when_enabled_and_up():
    t = render_nft6(_plan(ipv6_enabled=True), tunnel_up=True)
    assert "chain prerouting" in t and "tproxy ip6 to :52346" in t
    assert "meta mark 0x80 return" in t                 # anti-loop egress mark
    assert "udp dport { 546, 547 } return" in t          # DHCPv6 carve-out
    assert "ipv6-icmp" not in t                          # C: misleading icmpv6 bypass line removed
    # A: forward-drop backstop — non-tcp/udp (ICMPv6) global v6 is dropped, not leaked
    assert "chain forward" in t
    assert 'iifname "eth0.2" ip6 daddr != { ::1/128, fe80::/10, fc00::/7, ff00::/8 } drop' in t


def test_render_nft6_carves_out_static_segment_prefix():
    t = render_nft6(_plan(ipv6_enabled=True, segment_ip6="2001:db8:0:2::1/64"), tunnel_up=True)
    assert "2001:db8:0:2::/64" in t            # B: segment /64 bypassed → intra-segment v6 stays local
    # auto / blank → no segment carve-out (documented limitation until observed)
    assert "2001:db8" not in render_nft6(_plan(ipv6_enabled=True, segment_ip6="auto"), tunnel_up=True)


def test_apply_v6_sets_accept_ra_on_uplink():
    writes = []
    be = LinuxBackend(Settings(), run=FakeRun(), write_proc=lambda p, v: writes.append((p, v)))
    be.apply_tproxy(_plan(ipv6_enabled=True))
    assert ("/proc/sys/net/ipv6/conf/all/forwarding", "1") in writes
    assert ("/proc/sys/net/ipv6/conf/eth0/accept_ra", "2") in writes   # D: keep RA on the Home leg
    # v4-only apply touches no v6 sysctls
    w4 = []
    LinuxBackend(Settings(), run=FakeRun(), write_proc=lambda p, v: w4.append((p, v))).apply_tproxy(
        NetPlan.from_settings(Settings()))
    assert ("/proc/sys/net/ipv4/ip_forward", "1") in w4 and not any("ipv6" in p for p, _ in w4)


def test_segment_prefix6_skips_temporary_address():
    sample = (
        "20010db8000000020000000000000abc 03 40 00 01 eth0.2\n"   # flags 01 = temporary → skip
        "20010db8000000020000000000000001 03 40 00 00 eth0.2\n"   # stable → match
    )
    assert netcheck.segment_prefix6("eth0.2", read=lambda: sample) == "2001:db8:0:2::1/64"


def test_render_nft6_guard_when_enabled_but_tunnel_down_and_killswitch():
    t = render_nft6(_plan(ipv6_enabled=True, kill_switch=True), tunnel_up=False)
    assert "chain forward" in t and " drop" in t and "tproxy" not in t


def test_render_nft6_block_when_off_but_killswitch():
    t = render_nft6(_plan(kill_switch=True), tunnel_up=True)   # ipv6 off → leak-guard drop
    assert "chain forward" in t and " drop" in t and "tproxy" not in t


def test_render_nft6_empty_when_off_and_no_killswitch():
    assert render_nft6(_plan(), tunnel_up=True) == ""
    assert render_nft6(_plan(ipv6_enabled=True), tunnel_up=False) == ""   # enabled but down, no ks


# --- LinuxBackend v6 policy routing ----------------------------------------

def test_linux_apply_installs_v6_tproxy_and_routing_when_enabled():
    fake = FakeRun()
    LinuxBackend(Settings(), run=fake).apply_tproxy(_plan(ipv6_enabled=True))
    nft = next(i for c, i in fake.calls if c[:2] == ["nft", "-f"])
    assert "table ip6 pi_gw_panel" in nft and "tproxy ip6 to :52346" in nft
    cmds = fake.cmds()
    assert ["ip", "-6", "rule", "add", "fwmark", "0x40", "lookup", "100"] in cmds
    assert ["ip", "-6", "route", "replace", "local", "default", "dev", "lo", "table", "100"] in cmds


def test_linux_apply_cleans_stale_v6_routing_when_off():
    fake = FakeRun()
    LinuxBackend(Settings(), run=fake).apply_tproxy(NetPlan.from_settings(Settings()))
    cmds = fake.cmds()
    assert ["ip", "-6", "rule", "add", "fwmark", "0x40", "lookup", "100"] not in cmds   # no v6 tproxy
    assert ["ip", "-6", "rule", "del", "fwmark", "0x40", "lookup", "100"] in cmds        # E: stale cleanup


def test_linux_teardown_flushes_v6_routing():
    fake = FakeRun()
    LinuxBackend(Settings(), run=fake).teardown()
    assert ["ip", "-6", "rule", "del", "fwmark", "0x40", "lookup", "100"] in fake.cmds()
    assert ["ip", "-6", "route", "flush", "table", "100"] in fake.cmds()


# --- xray builder ----------------------------------------------------------

def _node():
    return Node(id=None, name="n", address="1.1.1.1", port=443, uuid="u")


def test_builder_adds_tproxy_in6_only_when_enabled():
    assert not any(i["tag"] == "tproxy-in6" for i in build_config(_node(), Settings())["inbounds"])
    cfg = build_config(_node(), Settings(), ipv6_tproxy=True)
    in6 = [i for i in cfg["inbounds"] if i["tag"] == "tproxy-in6"]
    assert in6 and in6[0]["listen"] == "::" and in6[0]["port"] == 52346
    assert in6[0]["streamSettings"]["sockopt"]["tproxy"] == "tproxy"


def test_builder_dns_intercept_covers_v6_inbound():
    cfg = build_config(_node(), Settings(), dns_intercept=True, ipv6_tproxy=True)
    dns_rule = next(r for r in cfg["routing"]["rules"] if r.get("outboundTag") == "dns-out")
    assert set(dns_rule["inboundTag"]) == {"tproxy-in", "tproxy-in6"}


# --- NetPlan resolution + recommendations ----------------------------------

def test_netplan_resolves_ipv6_from_store():
    store = _store()
    p0 = NetPlan.from_store(store, Settings())
    assert p0.ipv6_enabled is False and p0.segment_ip6 == "" and p0.tproxy_port6 == 52346
    store.set_setting("ipv6_enabled", "1")
    store.set_setting("segment_ip6", "2001:db8:0:2::1/64")
    p1 = NetPlan.from_store(store, Settings())
    assert p1.ipv6_enabled is True and p1.segment_ip6 == "2001:db8:0:2::1/64"


def test_recommendations_gate_on_ipv6():
    base = netcheck.router_recommendations(Settings())
    assert not any("/64" in r["title"] for r in base)
    v6 = netcheck.router_recommendations(Settings(), ipv6_enabled=True, segment_ip6="2001:db8:0:2::/64")
    assert any("/64" in r["title"] for r in v6)                       # the prefix-delegation hint
    assert any("2001:db8:0:2::/64" in r["detail"] for r in v6)        # references the configured /64
    assert any("RA" in r["detail"] or "radvd" in r["detail"] for r in v6)   # the host-RA caveat


# --- API: toggle rebuilds the live config ----------------------------------

def _client(settings, stub_xray):
    settings.xray_bin = stub_xray
    state = build_state(settings, net=DryRunBackend())
    c = TestClient(create_app(settings, state=state))
    c.post("/api/setup", json={"username": "admin", "password": "s3cret12"})
    return c, {"X-CSRF-Token": c.get("/api/csrf").json()["csrf"]}


# --- DHCPv6-PD 'auto' (spec §9) --------------------------------------------

def test_segment_prefix6_reads_global_scope_only():
    sample = (
        "fe800000000000000000000000000001 03 40 20 80 eth0.2\n"   # link-local (scope 20) → skip
        "20010db8000000020000000000000001 03 40 00 00 eth0.2\n"   # global (scope 00) → match
        "20010db8000000990000000000000001 03 40 00 00 eth0.9\n"   # other iface → skip
    )
    assert netcheck.segment_prefix6("eth0.2", read=lambda: sample) == "2001:db8:0:2::1/64"
    assert netcheck.segment_prefix6("eth0.7", read=lambda: sample) is None
    assert netcheck.segment_prefix6("eth0.2", read=lambda: "") is None


def test_recommendations_auto_mode_recommends_pd_client():
    v6 = netcheck.router_recommendations(Settings(), ipv6_enabled=True, segment_ip6="auto")
    assert any("DHCPv6-PD client" in r["title"] for r in v6)
    assert any("odhcp6c" in r["detail"] for r in v6)
    # static mode keeps the delegate-/64 wording
    static = netcheck.router_recommendations(Settings(), ipv6_enabled=True, segment_ip6="2001:db8::/64")
    assert any("Delegate an IPv6 /64" in r["title"] for r in static)


def test_put_network_auto_prefix_persists_and_recommends_pd(settings, stub_xray):
    c, h = _client(settings, stub_xray)
    body = c.put("/api/network", json={"ipv6_enabled": True, "segment_ip6": "auto"}, headers=h).json()
    assert body["segment"]["ip6"] == "auto"
    assert any("DHCPv6-PD client" in r["title"] for r in body["recommendations"])
    assert body["status"]["ipv6_prefix"] is None   # DryRun backend → prefix not observed


def test_put_network_validates_segment_ip6(settings, stub_xray):
    c, h = _client(settings, stub_xray)
    assert c.put("/api/network", json={"segment_ip6": "not-a-prefix"}, headers=h).status_code == 422
    assert c.put("/api/network", json={"segment_ip6": "2001:db8:0:2::/64"}, headers=h).status_code == 200
    assert c.put("/api/network", json={"segment_ip6": "auto"}, headers=h).status_code == 200
    assert c.put("/api/network", json={"segment_ip6": ""}, headers=h).status_code == 200   # clears


def test_network_payload_uplink6_none_on_dryrun(settings, stub_xray):
    c, h = _client(settings, stub_xray)
    c.put("/api/network", json={"ipv6_enabled": True}, headers=h)
    assert c.get("/api/network").json()["status"]["uplink6"] is None   # DryRun → v6 uplink not probed


def test_put_network_enable_ipv6_rebuilds_config_and_shows_hint(settings, stub_xray):
    c, h = _client(settings, stub_xray)
    nid = c.post("/api/nodes", json={"name": "n", "address": "1.2.3.4", "port": 443, "uuid": "u-1"},
                 headers=h).json()["id"]
    assert c.post(f"/api/nodes/{nid}/apply", headers=h).status_code == 200   # live tunnel
    body = c.put("/api/network",
                 json={"ipv6_enabled": True, "segment_ip6": "2001:db8:0:2::1/64"}, headers=h).json()
    assert body["ipv6_enabled"] is True and body["segment"]["ip6"] == "2001:db8:0:2::1/64"
    assert any("/64" in r["title"] for r in body["recommendations"])   # the router hint (the ask)
    assert any(e["kind"] == "ipv6" for e in body["events"])
    # the rebuilt live xray config now carries the v6 tproxy inbound
    cfg = json.load(open(settings.config_path))
    assert any(i["tag"] == "tproxy-in6" for i in cfg["inbounds"])
