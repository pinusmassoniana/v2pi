from pi_gw_panel.config import Settings
from pi_gw_panel.models import Node
from pi_gw_panel.xray_config.builder import build_config
from pi_gw_panel.xray_config.validate import validate_config


def _node(**kw) -> Node:
    base = dict(id=1, name="n", address="1.2.3.4", port=443, uuid="u",
                sni="s", public_key="PK", short_id="sid")
    base.update(kw)
    return Node(**base)


def test_stats_none_is_unchanged():
    cfg = build_config(_node(), Settings())
    assert "stats" not in cfg and "api" not in cfg and "policy" not in cfg
    assert all(i["tag"] != "api" for i in cfg["inbounds"])
    assert not any(r.get("inboundTag") == ["api"] for r in cfg["routing"]["rules"])


def test_stats_block_present_when_enabled():
    cfg = build_config(_node(), Settings(), stats={"api_port": 10085})
    assert cfg["stats"] == {}
    assert cfg["policy"]["system"]["statsOutboundUplink"] is True
    assert cfg["policy"]["system"]["statsOutboundDownlink"] is True
    assert cfg["api"] == {"tag": "api", "services": ["StatsService"]}
    api_in = next(i for i in cfg["inbounds"] if i["tag"] == "api")
    assert api_in["protocol"] == "dokodemo-door"
    assert api_in["listen"] == "127.0.0.1" and api_in["port"] == 10085
    # the api routing rule is dispatched first
    assert cfg["routing"]["rules"][0] == {"type": "field", "inboundTag": ["api"], "outboundTag": "api"}


def test_stats_config_validates_with_stub(settings, stub_xray):
    cfg = build_config(_node(), settings, stats={"api_port": 10085}, tunneled_fetch=True)
    ok, _ = validate_config(cfg, stub_xray)
    assert ok is True
