"""Leak-shaped regressions in the emitted xray config.

Every test here fails if a defect that silently sent client traffic somewhere it should not go
comes back. They assert emitted fields and rule ORDER, because in xray the first matching rule
wins — a correct-looking rule sitting behind another one is the whole bug class.
"""
import ipaddress
import logging

import pytest

from pi_gw_panel import rw_inbound as rw
from pi_gw_panel.config import Settings
from pi_gw_panel.models import Node, RoutingRule, TuningProfile
from pi_gw_panel.xray_config.builder import build_config
from pi_gw_panel.xray_config.routing import validate_routing
from pi_gw_panel.xray_config.tuning import validate_profile


def _node(**kw) -> Node:
    base = dict(id=1, name="n1", address="1.2.3.4", port=47000, uuid="u-1",
                sni="www.microsoft.com", public_key="PK", short_id="ab12",
                flow="xtls-rprx-vision")
    base.update(kw)
    return Node(**base)


def _rw(**over) -> dict:
    base = {"port": 443, "dest": "www.microsoft.com:443",
            "server_names": ["www.microsoft.com"], "private_key": "PRIV",
            "short_ids": ["ab12cd34"], "clients": [{"id": "c-1", "email": "iphone"}],
            "hosts": {}}
    base.update(over)
    return base


def _rules(cfg) -> list[dict]:
    return cfg["routing"]["rules"]


def _index(cfg, pred) -> int:
    return next(i for i, r in enumerate(_rules(cfg)) if pred(r))


# The `private` list xray ships in geoip.dat: RFC1918 + loopback + link-local + "this network".
# Asserting against the emitted rule text alone cannot catch a MISSING alias — only replaying
# first-match-wins over the whole rule list shows where an address actually ends up.
_GEOIP_PRIVATE = ["0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
                  "192.168.0.0/16", "::1/128", "fc00::/7", "fe80::/10"]


def _route(cfg, inbound_tag: str, dest: str) -> str:
    """Which outbound an IP destination from `inbound_tag` reaches — first match wins."""
    ip = ipaddress.ip_address(dest)
    for rule in _rules(cfg):
        tags = rule.get("inboundTag")
        if tags is not None and inbound_tag not in tags:
            continue
        if any(k in rule for k in ("protocol", "domain", "port")):
            continue                       # not selected by a plain IP destination
        nets = rule.get("ip")
        if nets is None:
            return rule["outboundTag"]     # the network catch-all
        for entry in nets:
            for cidr in (_GEOIP_PRIVATE if entry == "geoip:private" else [entry]):
                net = ipaddress.ip_network(cidr)
                if ip.version == net.version and ip in net:
                    return rule["outboundTag"]
    return "(no rule matched)"


# --- 1. LAN-by-name must not hand a whole TLD to the plain freedom outbound ---------------

def test_mapped_host_emits_exact_name_never_a_tld_suffix_rule():
    """`domain:com` matches `com` AND every subdomain of it, ahead of the catch-all — one
    mapped `nas.example.com` would send every .com destination out `direct-lan` (plain
    freedom, no tunnel). Only the exact mapped names may be routed there."""
    cfg = build_config(_node(), Settings(),
                       rw_inbound=_rw(hosts={"nas.example.com": "192.168.1.88"}))
    lan = next(r for r in _rules(cfg) if r.get("outboundTag") == "direct-lan")

    assert lan["domain"] == ["full:nas.example.com"]
    for rule in _rules(cfg):
        assert "domain:com" not in (rule.get("domain") or [])
        assert not any(d.startswith("domain:") for d in (rule.get("domain") or []))


def test_mapped_hosts_emit_one_exact_rule_each_and_stay_before_the_catch_all():
    cfg = build_config(_node(), Settings(),
                       rw_inbound=_rw(hosts={"pve.v2pi": "192.168.1.101",
                                             "nas.v2pi": "192.168.1.88"}))
    rules = _rules(cfg)
    assert rules[-2] == {"type": "field",
                         "domain": ["full:nas.v2pi", "full:pve.v2pi"],
                         "outboundTag": "direct-lan"}
    assert rules[-1]["outboundTag"] == "proxy"          # catch-all still last


# What a mapped name may and may not capture is asserted above, and what validate_hosts accepts
# lives with the rest of the host-map contract in test_rw_inbound.py.


# --- 2. no silent plaintext resolver -----------------------------------------------------

def test_doh_enabled_config_has_no_plaintext_fallback_server():
    cfg = build_config(_node(), Settings())
    assert cfg["dns"]["servers"] == [{"address": Settings().doh_url}]
    assert "localhost" not in cfg["dns"]["servers"]


def test_disabled_doh_falls_back_to_the_host_resolver_but_never_silently(caplog):
    """`dns.servers == ["localhost"]` means every destination domain xray resolves goes to the
    host/ISP resolver in cleartext. That may be what the operator asked for — it may never be
    something they are not told about."""
    off = TuningProfile(id=1, name="no-doh", doh_enabled=False)
    with caplog.at_level(logging.WARNING, logger="pi_gw_panel.xray_config.builder"):
        cfg = build_config(_node(), Settings(), profile=off)

    assert cfg["dns"]["servers"] == ["localhost"]
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a plaintext DNS fallback must be reported, not applied silently"
    assert "cleartext" in warnings[0].getMessage()
    assert "no-doh" in warnings[0].getMessage()


def test_enabled_doh_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING, logger="pi_gw_panel.xray_config.builder"):
        build_config(_node(), Settings(), profile=TuningProfile(id=1, name="p", doh_enabled=True))
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# --- 3. client-facing inbounds may not reach the gateway's loopback services -------------

def test_loopback_guard_is_the_first_rule_and_precedes_private_direct():
    """`rw-in` sniffs with routeOnly, so a remote client can name `127.0.0.1` with no DNS trick;
    `geoip:private → direct` would then serve it through plain freedom, straight into the
    unauthenticated sub-fetch proxy and the xray gRPC api."""
    cfg = build_config(_node(), Settings(), ipv6_tproxy=True, rw_inbound=_rw())
    rules = _rules(cfg)
    guard = rules[0]

    assert guard["outboundTag"] == "block"
    assert set(guard["inboundTag"]) == {"tproxy-in", "tproxy-in6", "rw-in"}
    assert "127.0.0.0/8" in guard["ip"] and "::1/128" in guard["ip"]
    assert "169.254.0.0/16" in guard["ip"] and "fe80::/10" in guard["ip"]
    assert "0.0.0.0/8" in guard["ip"] and "::/128" in guard["ip"]
    assert 0 < _index(cfg, lambda r: r.get("ip") == ["geoip:private"])


def test_the_unspecified_address_reaches_the_sub_fetch_proxy_and_must_be_blocked_too():
    """`0.0.0.0:10808` is delivered to the SAME listener as `127.0.0.1:10808` on Linux, so a
    guard that names only the canonical loopback prefix still hands a remote client the
    unauthenticated sub-fetch proxy — through `geoip:private → direct`, a plain freedom
    outbound. Every alias of "the gateway itself" has to be in the guard, not just one."""
    cfg = build_config(_node(), Settings(), ipv6_tproxy=True, tunneled_fetch=True,
                       stats={"api_port": 10085}, rw_inbound=_rw())

    sub = next(i for i in cfg["inbounds"] if i["tag"] == "sub-fetch")
    assert (sub["listen"], sub["port"]) == ("127.0.0.1", Settings().local_proxy_port)  # :10808
    api = next(i for i in cfg["inbounds"] if i["tag"] == "api")
    assert api["listen"] == "127.0.0.1"

    for inbound in ("rw-in", "tproxy-in", "tproxy-in6"):
        for alias in ("127.0.0.1", "0.0.0.0", "0.0.0.1", "::1", "::"):
            assert _route(cfg, inbound, alias) == "block", f"{inbound} -> {alias}"


def test_the_loopback_guard_does_not_blackhole_lan_or_public_destinations():
    """The guard is only correct if it stayed narrow: LAN still goes direct, everything else
    still goes out the tunnel."""
    cfg = build_config(_node(), Settings(), ipv6_tproxy=True, rw_inbound=_rw())

    assert _route(cfg, "rw-in", "192.168.1.50") == "direct"
    assert _route(cfg, "tproxy-in", "10.8.0.3") == "direct"
    assert _route(cfg, "tproxy-in", "1.1.1.1") == "proxy"
    assert _route(cfg, "tproxy-in6", "2606:4700:4700::1111") == "proxy"


def test_loopback_guard_is_present_without_the_road_warrior_inbound():
    """A sniffed domain that resolves to loopback steers `tproxy-in` there too (rebinding)."""
    cfg = build_config(_node(), Settings())
    guard = _rules(cfg)[0]
    assert guard["inboundTag"] == ["tproxy-in"] and guard["outboundTag"] == "block"


def test_loopback_guard_survives_user_routing_stats_and_dns_intercept():
    """Everything else that prepends a rule must still land behind the guard."""
    rules = [RoutingRule(id=None, position=0, type="ip", value="1.2.3.0/24", action="direct")]
    cfg = build_config(_node(), Settings(), routing=(rules, "proxy"),
                       stats={"api_port": 10085}, dns_intercept=True)
    assert _rules(cfg)[0]["outboundTag"] == "block"
    assert {"127.0.0.0/8", "0.0.0.0/8"} <= set(_rules(cfg)[0]["ip"])
    # the api dispatch and :53 interception rules are untouched, just no longer first
    assert _index(cfg, lambda r: r.get("inboundTag") == ["api"]) > 0
    assert _index(cfg, lambda r: r.get("outboundTag") == "dns-out") > 0


# --- 4. a rule the router can never reach is refused, not silently ignored ----------------

def test_private_range_rule_with_a_non_direct_action_is_rejected():
    """`geoip:private → direct` is rule #1, so `ip 10.8.0.0/24 → proxy` validates, displays as
    active and provably cannot fire; a `block` on a private range would go direct instead."""
    for action in ("proxy", "block"):
        rule = RoutingRule(id=None, position=0, type="ip", value="10.8.0.0/24", action=action)
        ok, err = validate_routing([rule], "proxy")
        assert ok is False, action
        assert "private" in err

    geo = RoutingRule(id=None, position=0, type="geoip", value="private", action="block")
    assert validate_routing([geo], "proxy")[0] is False


def test_private_range_rule_stays_valid_for_direct_and_public_ranges_are_untouched():
    """The shipped LAN-direct preset (private ranges → direct) agrees with the built-in rule,
    so it must keep validating; only unreachable actions are refused."""
    lan_direct = [RoutingRule(id=None, position=0, type="ip", value="192.168.0.0/16", action="direct"),
                  RoutingRule(id=None, position=1, type="ip", value="10.0.0.0/8", action="direct")]
    assert validate_routing(lan_direct, "proxy") == (True, "")

    public = RoutingRule(id=None, position=0, type="ip", value="8.8.8.0/24", action="proxy")
    assert validate_routing([public], "proxy") == (True, "")


# --- 5. the QUIC knob must not sit behind a user rule ------------------------------------

def test_quic_rule_precedes_every_user_rule():
    """Behind `geosite:google → proxy` the drop never fires and QUIC still reaches a node that
    may not relay it — precisely the blackhole the knob exists to prevent."""
    rules = [RoutingRule(id=None, position=0, type="geosite", value="google", action="proxy"),
             RoutingRule(id=None, position=1, type="domain", value="x.com", action="block")]
    cfg = build_config(_node(), Settings(), profile=TuningProfile(id=1, name="p", quic="drop"),
                       routing=(rules, "proxy"))

    quic_idx = _index(cfg, lambda r: r.get("protocol") == ["quic"])
    first_user = _index(cfg, lambda r: r.get("domain") == ["geosite:google"])
    private_idx = _index(cfg, lambda r: r.get("ip") == ["geoip:private"])

    assert _rules(cfg)[quic_idx]["outboundTag"] == "block"
    assert quic_idx == private_idx + 1                # immediately after private→direct
    assert quic_idx < first_user
    assert _rules(cfg)[-1]["outboundTag"] == "proxy"  # catch-all still last


# --- 6. the DoH URL is validated whenever it is set --------------------------------------

def test_bare_ip_doh_url_is_rejected_even_with_doh_disabled():
    """`dns_intercept` makes the stored URL the SOLE dns server regardless of the profile
    toggle, so a bare `8.8.8.8` would turn every client's DNS into plaintext UDP to a third
    party while the profile still reads "DoH off"."""
    bad = TuningProfile(id=1, name="p", doh_enabled=False, doh_url="8.8.8.8")
    ok, err = validate_profile(bad)
    assert ok is False and "DoH URL" in err

    good = TuningProfile(id=1, name="p", doh_enabled=False, doh_url="https://dns.google/dns-query")
    assert validate_profile(good)[0] is True
    empty = TuningProfile(id=1, name="p", doh_enabled=False, doh_url="")
    assert validate_profile(empty)[0] is True


def test_plaintext_http_doh_url_is_rejected_even_with_doh_disabled():
    """`http://` is not DoH. It used to validate, and `dns_intercept` then installed it as the
    SOLE dns server while the profile still read "DoH off" — every client's domains leaving the
    gateway in cleartext, to a third party, presented by the UI as the encrypted resolver."""
    for url in ("http://dns.example/dns-query", "http://8.8.8.8/dns-query", "https://"):
        for doh_on in (False, True):
            bad = TuningProfile(id=1, name="p", doh_enabled=doh_on, doh_url=url)
            ok, err = validate_profile(bad)
            assert ok is False, (url, doh_on)
            assert "https" in err


def test_build_refuses_to_install_a_plaintext_resolver_as_the_encrypted_one():
    """Validation only guards NEW writes. A profile stored before the rule, restored from an
    older backup, or hand-edited in the DB still reaches build_config — which is where the URL
    actually becomes dns.servers. It has to fail closed there too, or the class stays open."""
    stored = TuningProfile(id=1, name="p", doh_enabled=False,
                           doh_url="http://dns.example/dns-query")

    # the path that makes it the sole server despite the profile's own toggle being off
    with pytest.raises(ValueError, match="https"):
        build_config(_node(), Settings(), profile=stored, dns_intercept=True)
    # and the ordinary DoH-on path
    with pytest.raises(ValueError, match="https"):
        build_config(_node(), Settings(),
                     profile=TuningProfile(id=1, name="p", doh_enabled=True,
                                           doh_url="http://dns.example/dns-query"))
    # a non-https resolver configured in the environment is the same leak, not an exemption
    with pytest.raises(ValueError, match="https"):
        build_config(_node(), Settings(doh_url="http://1.1.1.1/dns-query"))

    # ...but the guard only fires where the URL is actually used: DoH off and no interception
    # still takes the documented (warned-about) host-resolver path.
    off = build_config(_node(), Settings(), profile=stored)
    assert off["dns"]["servers"] == ["localhost"]
