"""Coverage for the Routing-panel audit fixes/features (RC1–RC3, RN1/RN3/RN5/RN9)."""
from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.models import RoutingRule
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.xray_config.routing import rules_to_xray, validate_routing, _split


def _store(settings):
    conn = connect(settings.db_path, check_same_thread=False)
    init_schema(conn)
    return NodeStore(conn)


# --- RN5: multi-value split ---
def test_multi_value_rule_becomes_list():
    r = RoutingRule(id=None, position=0, type="domain", value="a.com, b.com\nc.com", action="block")
    field = rules_to_xray([r], "proxy")[1]
    assert field["domain"] == ["a.com", "b.com", "c.com"] and field["outboundTag"] == "block"
    rip = RoutingRule(id=None, position=0, type="geoip", value="ru,cn", action="direct")
    assert rules_to_xray([rip], "proxy")[1]["ip"] == ["geoip:ru", "geoip:cn"]


# --- RN3: disabled rules are skipped ---
def test_disabled_rule_excluded():
    on = RoutingRule(id=None, position=0, type="domain", value="x.com", action="block")
    off = RoutingRule(id=None, position=1, type="domain", value="y.com", action="block", enabled=False)
    out = rules_to_xray([on, off], "proxy")
    domains = [r.get("domain") for r in out if "domain" in r]
    assert ["x.com"] in domains and ["y.com"] not in domains


# --- RC2: structural validation ---
def test_validate_routing_catches_bad_values():
    bad_port = [RoutingRule(id=None, position=0, type="port", value="abc", action="proxy")]
    ok, err = validate_routing(bad_port, "proxy")
    assert not ok and "port" in err
    bad_ip = [RoutingRule(id=None, position=0, type="ip", value="999.1", action="proxy")]
    assert validate_routing(bad_ip, "proxy")[0] is False
    assert validate_routing([], "nope")[0] is False              # bad default action
    good = [RoutingRule(id=None, position=0, type="ip", value="10.0.0.0/8", action="direct")]
    assert validate_routing(good, "proxy") == (True, "")


def test_port_rules_require_ordered_valid_tcp_ports():
    for value in ("0", "65536", "200-100", "0-443", "443-65536"):
        rule = RoutingRule(id=None, position=0, type="port", value=value, action="proxy")
        ok, error = validate_routing([rule], "proxy")
        assert ok is False, value
        assert "port" in error

    boundary = RoutingRule(id=None, position=0, type="port", value="1,65535,80-443", action="proxy")
    assert validate_routing([boundary], "proxy") == (True, "")


def test_split_helper():
    assert _split("a, b\nc ,, ") == ["a", "b", "c"]


# --- API ---
def _client(settings, stub_xray):
    settings.xray_bin = stub_xray
    from pi_gw_panel.state import build_state
    return TestClient(create_app(settings, state=build_state(settings, net=DryRunBackend())))


def _login(c):
    c.post("/api/setup", json={"username": "admin", "password": "changeme"})
    return c.get("/api/csrf").json()["csrf"]


def test_put_routing_rejects_bad_rule(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    r = c.put("/api/routing", json={"rules": [{"type": "port", "value": "nope", "action": "proxy"}],
                                    "default_action": "proxy"}, headers=h)
    assert r.status_code == 422 and "port" in r.json()["detail"]


def test_put_routing_drops_empty_and_dedupes(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    r = c.put("/api/routing", json={"rules": [
        {"type": "domain", "value": "x.com", "action": "block"},
        {"type": "domain", "value": "", "action": "proxy"},          # dropped
        {"type": "domain", "value": "x.com", "action": "block"},     # dup
    ], "default_action": "proxy"}, headers=h)
    assert r.status_code == 200
    assert [(x["type"], x["value"]) for x in r.json()["rules"]] == [("domain", "x.com")]


def test_preset_is_non_persisting(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    pre = c.post("/api/routing/preset/ru-direct", headers=h).json()
    assert any(x["type"] == "geosite" for x in pre["rules"])
    # importing did NOT persist — the stored ruleset is still empty until a Save
    assert c.get("/api/routing").json()["rules"] == []


def test_presets_list_and_unknown_404(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    names = [p["name"] for p in c.get("/api/routing/presets").json()]
    assert "ru-direct" in names and "block-ads" in names
    assert c.post("/api/routing/preset/nope", headers=h).status_code == 404


def test_routing_validate_endpoint(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    ok = c.post("/api/routing/validate", json={"rules": [{"type": "ip", "value": "1.2.3.0/24", "action": "direct"}],
                                               "default_action": "proxy"}, headers=h)
    assert ok.json()["ok"] is True
    bad = c.post("/api/routing/validate", json={"rules": [{"type": "ip", "value": "x", "action": "direct"}],
                                                "default_action": "proxy"}, headers=h)
    assert bad.json()["ok"] is False


def test_domain_strategy_round_trips(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    r = c.put("/api/routing", json={"rules": [], "default_action": "proxy",
                                    "domain_strategy": "AsIs"}, headers=h)
    assert r.json()["domain_strategy"] == "AsIs"
    assert c.get("/api/routing").json()["domain_strategy"] == "AsIs"


def test_rule_enabled_label_round_trip(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    r = c.put("/api/routing", json={"rules": [
        {"type": "domain", "value": "x.com", "action": "block", "enabled": False, "label": "ads"}],
        "default_action": "proxy"}, headers=h)
    rule = r.json()["rules"][0]
    assert rule["enabled"] is False and rule["label"] == "ads"
