from pi_gw_panel.config import Settings
from pi_gw_panel.models import Node
from pi_gw_panel.xray_config.builder import build_config


def _node():
    return Node(id=1, name="n1", address="1.2.3.4", port=47000, uuid="u-1",
                sni="www.microsoft.com", public_key="PK", short_id="ab12",
                flow="xtls-rprx-vision")


def test_dns_intercept_adds_dns_outbound_and_route():
    cfg = build_config(_node(), Settings(), dns_intercept=True)
    assert any(o.get("protocol") == "dns" and o.get("tag") == "dns-out" for o in cfg["outbounds"])
    r0 = cfg["routing"]["rules"][0]                       # client :53 (tproxy-in) → dns-out
    assert r0 == {"type": "field", "inboundTag": ["tproxy-in"], "port": 53, "outboundTag": "dns-out"}
    assert any(isinstance(s, dict) and "1.1.1.1" in s["address"] for s in cfg["dns"]["servers"])


def test_dns_intercept_off_by_default_is_unchanged():
    cfg = build_config(_node(), Settings())
    assert not any(o.get("tag") == "dns-out" for o in cfg["outbounds"])
    assert all(r.get("outboundTag") != "dns-out" for r in cfg["routing"]["rules"])


def test_tproxy_inbound_present():
    cfg = build_config(_node(), Settings())
    inb = cfg["inbounds"][0]
    assert inb["protocol"] == "dokodemo-door"
    assert inb["port"] == Settings().tproxy_port
    assert inb["streamSettings"]["sockopt"]["tproxy"] == "tproxy"
    assert set(inb["sniffing"]["destOverride"]) >= {"http", "tls"}


def test_proxy_outbound_is_vless_reality_vision():
    cfg = build_config(_node(), Settings())
    proxy = next(o for o in cfg["outbounds"] if o["tag"] == "proxy")
    assert proxy["protocol"] == "vless"
    vnext = proxy["settings"]["vnext"][0]
    assert vnext["address"] == "1.2.3.4"
    assert vnext["port"] == 47000
    user = vnext["users"][0]
    assert user["id"] == "u-1"
    assert user["flow"] == "xtls-rprx-vision"
    rs = proxy["streamSettings"]
    assert rs["security"] == "reality"
    assert rs["realitySettings"]["serverName"] == "www.microsoft.com"
    assert rs["realitySettings"]["publicKey"] == "PK"


def test_direct_block_and_doh_present():
    cfg = build_config(_node(), Settings())
    tags = {o["tag"] for o in cfg["outbounds"]}
    assert {"proxy", "direct", "block"} <= tags
    assert cfg["dns"]["servers"][0]["address"] == "https://1.1.1.1/dns-query"


def test_routing_sends_default_to_proxy_private_to_direct():
    cfg = build_config(_node(), Settings())
    rules = cfg["routing"]["rules"]
    private = next(r for r in rules if r.get("ip") == ["geoip:private"])
    assert private["outboundTag"] == "direct"
    assert rules[-1]["outboundTag"] == "proxy"  # catch-all last


def test_builder_threads_custom_settings():
    s = Settings(tproxy_port=10000, doh_url="https://dns.google/dns-query")
    cfg = build_config(_node(), s)
    assert cfg["inbounds"][0]["port"] == 10000
    assert cfg["dns"]["servers"][0]["address"] == "https://dns.google/dns-query"


def test_egress_mark_on_inbound_and_outbounds():
    cfg = build_config(_node(), Settings())
    assert cfg["inbounds"][0]["streamSettings"]["sockopt"]["mark"] == Settings().egress_mark
    proxy = next(o for o in cfg["outbounds"] if o["tag"] == "proxy")
    assert proxy["streamSettings"]["sockopt"]["mark"] == Settings().egress_mark
    direct = next(o for o in cfg["outbounds"] if o["tag"] == "direct")
    assert direct["streamSettings"]["sockopt"]["mark"] == Settings().egress_mark
