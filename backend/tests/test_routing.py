from pi_gw_panel.config import Settings
from pi_gw_panel.models import Node, RoutingRule, TuningProfile
from pi_gw_panel.xray_config.builder import build_config
from pi_gw_panel.xray_config.routing import rules_to_xray, RU_DIRECT_PRESET
from pi_gw_panel.xray_config.validate import validate_config


def _node(**kw) -> Node:
    base = dict(id=1, name="n", address="1.2.3.4", port=443, uuid="u",
                sni="s", public_key="PK", short_id="sid")
    base.update(kw)
    return Node(**base)


def test_rules_to_xray_structure_and_order():
    rules = [
        RoutingRule(id=None, position=0, type="geoip", value="ru", action="direct"),
        RoutingRule(id=None, position=1, type="geosite", value="category-ads-all", action="block"),
        RoutingRule(id=None, position=2, type="domain", value="example.com", action="proxy"),
        RoutingRule(id=None, position=3, type="ip", value="1.2.3.0/24", action="direct"),
        RoutingRule(id=None, position=4, type="port", value="443", action="proxy"),
    ]
    out = rules_to_xray(rules, "block")
    assert out[0] == {"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"}
    assert out[1] == {"type": "field", "ip": ["geoip:ru"], "outboundTag": "direct"}
    assert out[2] == {"type": "field", "domain": ["geosite:category-ads-all"], "outboundTag": "block"}
    assert out[3] == {"type": "field", "domain": ["example.com"], "outboundTag": "proxy"}
    assert out[4] == {"type": "field", "ip": ["1.2.3.0/24"], "outboundTag": "direct"}
    assert out[5] == {"type": "field", "port": "443", "outboundTag": "proxy"}
    assert out[-1] == {"type": "field", "network": "tcp,udp", "outboundTag": "block"}


def test_rules_to_xray_sorts_by_position():
    rules = [
        RoutingRule(id=None, position=2, type="domain", value="b.com", action="proxy"),
        RoutingRule(id=None, position=1, type="domain", value="a.com", action="direct"),
    ]
    out = rules_to_xray(rules, "proxy")
    assert out[1]["domain"] == ["a.com"] and out[2]["domain"] == ["b.com"]


def test_empty_rules_proxy_default_is_wave0_routing():
    assert rules_to_xray([], "proxy") == [
        {"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"},
        {"type": "field", "network": "tcp,udp", "outboundTag": "proxy"},
    ]


def test_ru_direct_preset():
    out = rules_to_xray(RU_DIRECT_PRESET, "proxy")
    tags = [(r.get("ip") or r.get("domain"), r["outboundTag"]) for r in out[1:-1]]
    assert tags == [(["geoip:ru"], "direct"), (["geosite:category-ru"], "direct")]


def test_build_config_applies_routing():
    rules = [RoutingRule(id=None, position=0, type="domain", value="x.com", action="block")]
    cfg = build_config(_node(), Settings(), routing=(rules, "proxy"))
    assert {"type": "field", "domain": ["x.com"], "outboundTag": "block"} in cfg["routing"]["rules"]
    assert cfg["routing"]["rules"][-1]["outboundTag"] == "proxy"


def test_routing_and_quic_compose():
    rules = [RoutingRule(id=None, position=0, type="domain", value="x.com", action="block")]
    p = TuningProfile(id=1, name="p", quic="drop")
    cfg = build_config(_node(), Settings(), profile=p, routing=(rules, "direct"))
    rlist = cfg["routing"]["rules"]
    quic_idx = next(i for i, r in enumerate(rlist) if r.get("protocol") == ["quic"])
    assert rlist[quic_idx]["outboundTag"] == "block"
    assert quic_idx == len(rlist) - 2                     # right before the catch-all
    assert rlist[-1]["outboundTag"] == "direct"           # custom default action preserved


def test_routed_config_validates_with_stub(settings, stub_xray):
    rules = [RoutingRule(id=None, position=0, type="geoip", value="ru", action="direct")]
    cfg = build_config(_node(), settings, routing=(rules, "proxy"))
    ok, _ = validate_config(cfg, stub_xray)
    assert ok is True
