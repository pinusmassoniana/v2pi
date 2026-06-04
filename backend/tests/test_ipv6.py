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
    assert "meta l4proto ipv6-icmp return" in t         # NDP/RA must stay local
    assert "udp dport { 546, 547 } return" in t          # DHCPv6 carve-out
    assert 'iifname "eth0.2"' in t


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


def test_linux_apply_no_v6_routing_when_off():
    fake = FakeRun()
    LinuxBackend(Settings(), run=fake).apply_tproxy(NetPlan.from_settings(Settings()))
    assert not any(c[:2] == ["ip", "-6"] for c in fake.cmds())   # v4-only


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
