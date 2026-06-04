from pi_gw_panel.config import Settings
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.models import Node, TuningProfile
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.xray_config.builder import build_config
from pi_gw_panel.xray_config.tuning import resolve_profile
from pi_gw_panel.xray_config.validate import validate_config


def _node(**kw) -> Node:
    base = dict(id=1, name="n", address="1.2.3.4", port=443, uuid="u",
                sni="s", public_key="PK", short_id="sid")
    base.update(kw)
    return Node(**base)


# --- resolve_profile -------------------------------------------------------

def test_resolve_profile_prefers_node_then_default(settings):
    conn = connect(settings.db_path)
    init_schema(conn)
    s = NodeStore(conn)
    default = s.get_default_profile()
    # node with no explicit profile → global default
    assert resolve_profile(s, _node(tuning_profile_id=None)).id == default.id
    # node with an explicit profile → that one
    pid = s.add_profile(TuningProfile(id=None, name="custom", fingerprint="randomized"))
    assert resolve_profile(s, _node(tuning_profile_id=pid)).fingerprint == "randomized"
    # a dangling profile id falls back to the default
    assert resolve_profile(s, _node(tuning_profile_id=99999)).id == default.id
    # no store → None (Wave-0 path)
    assert resolve_profile(None, _node()) is None


# --- defaults == Wave-0 ----------------------------------------------------

def test_build_config_defaults_are_wave0():
    cfg = build_config(_node(), Settings())
    assert len(cfg["inbounds"]) == 1
    assert [o["tag"] for o in cfg["outbounds"]] == ["proxy", "direct", "block"]
    assert "mux" not in cfg["outbounds"][0]
    assert "dialerProxy" not in cfg["outbounds"][0]["streamSettings"]["sockopt"]
    assert cfg["outbounds"][0]["streamSettings"]["realitySettings"]["fingerprint"] == "chrome"
    assert cfg["routing"]["rules"][-1]["outboundTag"] == "proxy"           # catch-all last
    assert not any(r.get("protocol") == ["quic"] for r in cfg["routing"]["rules"])
    assert cfg["dns"]["servers"][0]["address"] == Settings().doh_url


# --- profile-driven knobs --------------------------------------------------

def test_profile_fingerprint_overrides_node():
    p = TuningProfile(id=1, name="p", fingerprint="randomized")
    cfg = build_config(_node(fingerprint="chrome"), Settings(), profile=p)
    assert cfg["outbounds"][0]["streamSettings"]["realitySettings"]["fingerprint"] == "randomized"


def test_profile_fragmentation_and_mux():
    p = TuningProfile(id=1, name="p", frag_enabled=True, frag_packets="tlshello",
                      frag_length="100-200", frag_interval="10-20", mux_enabled=True)
    cfg = build_config(_node(), Settings(), profile=p)   # vision node
    frag = next(o for o in cfg["outbounds"] if o["tag"] == "fragment")
    assert frag["settings"]["fragment"]["packets"] == "tlshello"
    assert cfg["outbounds"][0]["streamSettings"]["sockopt"]["dialerProxy"] == "fragment"
    assert "mux" not in cfg["outbounds"][0]               # TC1: mux invalid with XTLS Vision
    # mux IS emitted for a non-Vision (xhttp) node
    xcfg = build_config(_node(transport="xhttp", network="xhttp", security="tls", flow=""),
                        Settings(), profile=p)
    assert xcfg["outbounds"][0]["mux"] == {"enabled": True}


def test_profile_noises_and_xhttp_extra():
    p = TuningProfile(id=1, name="p", noise_enabled=True,
                      noises=[{"type": "rand", "packet": "50-150", "delay": "10-16"}],
                      xhttp_padding="100-1000", xmux_max_concurrency="16")
    cfg = build_config(_node(transport="xhttp", network="xhttp", security="tls", flow=""),
                       Settings(), profile=p)
    frag = next(o for o in cfg["outbounds"] if o["tag"] == "fragment")
    assert frag["settings"]["noises"][0]["type"] == "rand"
    extra = cfg["outbounds"][0]["streamSettings"]["xhttpSettings"]["extra"]
    assert extra["xPaddingBytes"] == "100-1000" and extra["xmux"]["maxConcurrency"] == "16"


def test_profile_doh_url_and_disable():
    on = TuningProfile(id=1, name="p", doh_enabled=True, doh_url="https://dns.google/dns-query")
    cfg = build_config(_node(), Settings(), profile=on)
    assert cfg["dns"]["servers"][0]["address"] == "https://dns.google/dns-query"
    off = TuningProfile(id=1, name="p", doh_enabled=False)
    cfg2 = build_config(_node(), Settings(), profile=off)
    assert all(not isinstance(srv, dict) for srv in cfg2["dns"]["servers"])  # only localhost


def test_quic_modes():
    drop = build_config(_node(), Settings(), profile=TuningProfile(id=1, name="p", quic="drop"))
    assert next(r for r in drop["routing"]["rules"]
                if r.get("protocol") == ["quic"])["outboundTag"] == "block"
    proxy = build_config(_node(), Settings(), profile=TuningProfile(id=1, name="p", quic="proxy"))
    assert next(r for r in proxy["routing"]["rules"]
                if r.get("protocol") == ["quic"])["outboundTag"] == "proxy"
    allow = build_config(_node(), Settings(), profile=TuningProfile(id=1, name="p", quic="allow"))
    assert not any(r.get("protocol") == ["quic"] for r in allow["routing"]["rules"])
    assert drop["routing"]["rules"][-1]["outboundTag"] == "proxy"          # catch-all still last


def test_tunneled_fetch_adds_local_http_inbound():
    cfg = build_config(_node(), Settings(local_proxy_port=10808), tunneled_fetch=True)
    http = next(i for i in cfg["inbounds"] if i["tag"] == "sub-fetch")
    assert http["protocol"] == "http" and http["listen"] == "127.0.0.1" and http["port"] == 10808
    assert cfg["inbounds"][0]["protocol"] == "dokodemo-door"               # tproxy still first


def test_full_profile_validates_with_stub(settings, stub_xray):
    p = TuningProfile(id=1, name="p", fingerprint="randomized", frag_enabled=True,
                      mux_enabled=True, quic="drop")
    cfg = build_config(_node(), settings, profile=p, tunneled_fetch=True)
    ok, _out = validate_config(cfg, stub_xray)
    assert ok is True
