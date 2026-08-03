"""Road-warrior inbound: config emission, client artifacts, and the boundaries that matter.

The load-bearing invariants here are negative ones — what must NOT happen: the Reality private
key must never reach the API, an empty client list must never emit an inbound (xray won't start),
the generated Shadowrocket .conf must never bypass private ranges, and with the feature off the
config must be byte-identical to what ships today.
"""
import json

import pytest
from fastapi.testclient import TestClient

from pi_gw_panel.app import create_app
from pi_gw_panel.config import Settings
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.models import Node
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.state import build_state
from pi_gw_panel.xray_config.builder import build_config
from pi_gw_panel import rw_inbound as rw

PRIV = "aGVsbG8td29ybGQtdGhpcy1pcy1ub3QtYS1yZWFsLWtleQ"
PUB = "cHVibGljLWtleS1wbGFjZWhvbGRlci1ub3QtcmVhbC1rZXk"


def _node():
    return Node(id=1, name="n1", address="1.2.3.4", port=47000, uuid="u-1",
                sni="www.microsoft.com", public_key="PK", short_id="ab12",
                flow="xtls-rprx-vision")


def _store():
    conn = connect(":memory:")
    init_schema(conn)
    return NodeStore(conn)


def _enabled_store(**over):
    s = _store()
    vals = {"rw_enabled": "1", "rw_private_key": PRIV, "rw_public_key": PUB,
            "rw_port": "443", "rw_dest": "www.microsoft.com:443",
            "rw_server_names": "www.microsoft.com", "rw_short_ids": "ab12cd34",
            "rw_endpoint": "home.example.org"}
    vals.update(over)
    for k, v in vals.items():
        s.set_setting(k, v)
    rw.add_client(s, "iphone")
    return s


def _client(settings, stub_xray):
    settings.xray_bin = stub_xray
    return TestClient(create_app(settings, state=build_state(settings, net=DryRunBackend())))


def _auth(c):
    c.post("/api/setup", json={"username": "admin", "password": "s3cret12"})
    return {"X-CSRF-Token": c.get("/api/csrf").json()["csrf"]}


# --- §9.1/§9.4: off ⇒ nothing changes ---------------------------------------------------

def test_rw_kwarg_default_is_inert_and_the_off_config_keeps_its_exact_shape():
    """Two separate claims, because the obvious version of this test only proves the first.

    Comparing build_config() against build_config(rw_inbound=None) shows the kwarg default is
    inert — but it would pass just as happily if the feature had rewritten the config
    unconditionally. So the shape of the off config is also pinned outright: tags, tag order,
    and the two routing rules requirement 3 depends on.
    """
    base = build_config(_node(), Settings())
    with_kwarg = build_config(_node(), Settings(), rw_inbound=None)
    assert json.dumps(base, sort_keys=True) == json.dumps(with_kwarg, sort_keys=True)

    assert [i["tag"] for i in base["inbounds"]] == ["tproxy-in"]
    assert [o["tag"] for o in base["outbounds"]] == ["proxy", "direct", "block"]
    assert "hosts" not in base["dns"]
    # requirement 3 rests on these two rules and their order; nothing here may touch them
    assert base["routing"]["rules"] == [
        {"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"},
        {"type": "field", "network": "tcp,udp", "outboundTag": "proxy"},
    ]


def test_resolve_returns_none_when_disabled():
    s = _enabled_store(rw_enabled="0")
    assert rw.resolve(s) is None


def test_resolve_returns_none_without_private_key():
    s = _enabled_store()
    s.set_setting("rw_private_key", "")
    assert rw.resolve(s) is None


# --- §9.3: enabled but clientless must not emit -----------------------------------------

def test_no_clients_emits_no_inbound():
    """xray refuses to start on a vless inbound with an empty `clients` list. Emitting one
    would turn "deleted my last remote client" into a total tunnel outage."""
    s = _enabled_store()
    rw.set_clients(s, [])
    assert rw.resolve(s) is None
    cfg = build_config(_node(), Settings(), rw_inbound=rw.resolve(s))
    assert not any(i["tag"] == "rw-in" for i in cfg["inbounds"])


def test_disabled_clients_do_not_count_as_clients():
    s = _enabled_store()
    rw.set_clients(s, [{"id": "x", "email": "iphone", "enabled": False}])
    assert rw.resolve(s) is None


# --- §9.2: the emitted inbound ----------------------------------------------------------

def test_inbound_shape():
    s = _enabled_store()
    cfg = build_config(_node(), Settings(), rw_inbound=rw.resolve(s))
    inb = next(i for i in cfg["inbounds"] if i["tag"] == "rw-in")
    assert inb["protocol"] == "vless" and inb["port"] == 443 and inb["listen"] == "0.0.0.0"
    assert inb["settings"]["decryption"] == "none"
    assert inb["settings"]["clients"][0]["flow"] == "xtls-rprx-vision"
    assert inb["settings"]["clients"][0]["email"] == "iphone"
    stream = inb["streamSettings"]
    # Vision is only valid on tcp+reality — a copy-paste of the outbound's transport logic
    # (which can be xhttp/tls) would produce a config xray rejects.
    assert stream["network"] == "tcp" and stream["security"] == "reality"
    rs = stream["realitySettings"]
    assert rs["privateKey"] == PRIV
    assert rs["serverNames"] == ["www.microsoft.com"]
    assert rs["shortIds"] == ["ab12cd34"]
    assert rs["dest"] == "www.microsoft.com:443"
    assert inb["sniffing"]["routeOnly"] is True
    # mux is never valid on an inbound
    assert "mux" not in inb


def test_inbound_is_appended_last_so_existing_tags_keep_their_index():
    s = _enabled_store()
    plain = build_config(_node(), Settings(), tunneled_fetch=True, stats={"api_port": 10085})
    with_rw = build_config(_node(), Settings(), tunneled_fetch=True, stats={"api_port": 10085},
                           rw_inbound=rw.resolve(s))
    assert [i["tag"] for i in with_rw["inbounds"]] == [i["tag"] for i in plain["inbounds"]] + ["rw-in"]


def test_inbound_survives_custom_routing_rules():
    """Requirement 3: LAN access must not depend on the proxy outbound. private→direct stays
    first whatever the user's routing says, so a dead node cannot cut LAN off."""
    s = _enabled_store()
    cfg = build_config(_node(), Settings(), routing=([], "block"), rw_inbound=rw.resolve(s))
    assert cfg["routing"]["rules"][0] == {"type": "field", "ip": ["geoip:private"],
                                          "outboundTag": "direct"}
    assert any(i["tag"] == "rw-in" for i in cfg["inbounds"])


# --- LAN-by-name (Addendum A.3) ---------------------------------------------------------

def test_no_hosts_means_no_dns_hosts_no_direct_lan_no_domain_rule():
    s = _enabled_store()
    cfg = build_config(_node(), Settings(), rw_inbound=rw.resolve(s))
    assert "hosts" not in cfg["dns"]
    assert not any(o["tag"] == "direct-lan" for o in cfg["outbounds"])
    assert all("domain" not in r for r in cfg["routing"]["rules"])


def test_hosts_add_dns_hosts_direct_lan_and_rule_before_catch_all():
    s = _enabled_store()
    s.set_setting("rw_hosts", json.dumps({"nas.v2pi": "192.168.1.88",
                                          "pve.v2pi": "192.168.1.101"}))
    cfg = build_config(_node(), Settings(), rw_inbound=rw.resolve(s))
    assert cfg["dns"]["hosts"] == {"nas.v2pi": "192.168.1.88", "pve.v2pi": "192.168.1.101"}
    lan = next(o for o in cfg["outbounds"] if o["tag"] == "direct-lan")
    # Only UseIP resolves through xray's own DNS, where dns.hosts lives.
    assert lan["settings"]["domainStrategy"] == "UseIP"
    # the global direct must stay AsIs — flipping it pushes every RU domain through DoH
    assert next(o for o in cfg["outbounds"] if o["tag"] == "direct")["settings"] == {}
    rules = cfg["routing"]["rules"]
    assert rules[-2] == {"type": "field", "domain": ["domain:v2pi"], "outboundTag": "direct-lan"}
    assert rules[-1]["network"] == "tcp,udp"        # catch-all is still last


def test_local_suffix_is_rejected():
    """iOS/macOS answer .local over mDNS and never hand it to the proxy — accepting it would
    ship a config that silently never works."""
    with pytest.raises(ValueError, match="mDNS"):
        rw.validate_hosts({"nas.local": "192.168.1.88"})


def test_host_validation_rejects_bare_labels_and_bad_ips():
    with pytest.raises(ValueError):
        rw.validate_hosts({"nas": "192.168.1.88"})
    with pytest.raises(ValueError):
        rw.validate_hosts({"nas.v2pi": "not-an-ip"})


def test_short_id_validation():
    assert rw.validate_short_ids(["ab12", "0011223344556677"]) == ["ab12", "0011223344556677"]
    for bad in (["abc"], ["zz"], ["001122334455667788"], [""]):
        with pytest.raises(ValueError):
            rw.validate_short_ids(bad)


# --- §9.5: the vless:// link ------------------------------------------------------------

def test_link_format():
    s = _enabled_store()
    client = rw.get_clients(s)[0]
    link = rw.link(s, client)
    assert link.startswith(f"vless://{client['id']}@home.example.org:443?")
    for part in ("type=tcp", "security=reality", "encryption=none",
                 "flow=xtls-rprx-vision", "fp=chrome", "sni=www.microsoft.com",
                 f"pbk={PUB}", "sid=ab12cd34"):
        assert part in link
    assert link.endswith("#iphone")


def test_link_requires_endpoint_and_public_key():
    s = _enabled_store()
    s.set_setting("rw_endpoint", "")
    with pytest.raises(ValueError, match="endpoint"):
        rw.link(s, rw.get_clients(s)[0])
    s.set_setting("rw_endpoint", "home.example.org")
    s.set_setting("rw_public_key", "")
    with pytest.raises(ValueError, match="public key"):
        rw.link(s, rw.get_clients(s)[0])


# --- Addendum A.5: the Shadowrocket .conf -----------------------------------------------

def test_shadowrocket_conf_golden():
    """Pinned in full. The [Proxy] Reality key names are not in any official reference — they
    come from working third-party configs — so this is the one place a real-device correction
    lands, and it should be a visible one-line diff when it does."""
    s = _enabled_store()
    s.set_setting("rw_hosts", json.dumps({"nas.v2pi": "192.168.1.88"}))
    conf = rw.shadowrocket_conf(s, rw.get_clients(s)[0],
                                ["192.168.1.0/24", "192.168.10.0/24"])
    client_id = rw.get_clients(s)[0]["id"]
    assert conf == (
        "#!name = iphone\n"
        "# generated by pi-gw-panel — import into Shadowrocket\n"
        "\n"
        "[General]\n"
        "bypass-system = true\n"
        "ipv6 = false\n"
        "skip-proxy = 127.0.0.1, ::1, localhost, *.local\n"
        "\n"
        "[Proxy]\n"
        f"iphone = vless, home.example.org, 443, username={client_id}, tls=true, "
        "network=tcp, flow=xtls-rprx-vision, sni=www.microsoft.com, fingerprint=chrome, "
        f"reality-public-key={PUB}, reality-short-id=ab12cd34, udp=true\n"
        "\n"
        "[Rule]\n"
        "DOMAIN-SUFFIX,v2pi,iphone\n"
        "IP-CIDR,192.168.1.0/24,iphone,no-resolve\n"
        "IP-CIDR,192.168.10.0/24,iphone,no-resolve\n"
        "FINAL,iphone\n"
    )


def test_conf_never_bypasses_private_ranges():
    """Every client ships a default "bypass LAN" rule; with it 192.168.1.88 means the cafe's
    network, not ours. Reproducing that bypass in our own generated file would silently
    defeat requirement 1."""
    s = _enabled_store()
    conf = rw.shadowrocket_conf(s, rw.get_clients(s)[0], ["192.168.1.0/24"])
    skip = next(l for l in conf.splitlines() if l.startswith("skip-proxy"))
    for private in ("192.168.", "10.", "172.16.", "/24", "/16", "/12", "/8"):
        assert private not in skip


def test_conf_has_no_host_section():
    """Mapping names locally would make Shadowrocket forward an IP instead of the name,
    putting us back on IP routing and back in subnet-collision range."""
    s = _enabled_store()
    s.set_setting("rw_hosts", json.dumps({"nas.v2pi": "192.168.1.88"}))
    conf = rw.shadowrocket_conf(s, rw.get_clients(s)[0], ["192.168.1.0/24"])
    assert "[Host]" not in conf


def test_conf_every_ip_cidr_rule_carries_no_resolve():
    s = _enabled_store()
    conf = rw.shadowrocket_conf(s, rw.get_clients(s)[0], ["192.168.1.0/24", "10.9.0.0/16"])
    cidr = [l for l in conf.splitlines() if l.startswith("IP-CIDR")]
    assert len(cidr) == 2 and all(l.endswith(",no-resolve") for l in cidr)


def test_conf_never_leaks_the_private_key():
    s = _enabled_store()
    conf = rw.shadowrocket_conf(s, rw.get_clients(s)[0], ["192.168.1.0/24"])
    assert PRIV not in conf and PUB in conf


# --- §9.7/§9.8: API ---------------------------------------------------------------------

def test_get_rw_defaults(settings, stub_xray):
    c = _client(settings, stub_xray)
    _auth(c)
    body = c.get("/api/rw").json()
    assert body["enabled"] is False and body["port"] == 443
    assert body["has_private_key"] is False and body["clients"] == []
    assert body["live"] is False            # no active node ⇒ nothing rebuilt it yet


def test_private_key_never_appears_in_any_response(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = _auth(c)
    c.put("/api/rw", json={"enabled": False, "private_key": PRIV, "public_key": PUB,
                           "endpoint": "home.example.org", "short_ids": "ab12cd34"}, headers=h)
    created = c.post("/api/rw/clients", json={"email": "iphone"}, headers=h).json()
    cid = created["clients"][0]["id"]
    for path in ("/api/rw", f"/api/rw/clients/{cid}/link",
                 f"/api/rw/clients/{cid}/config", "/api/diagnostics", "/api/settings"):
        resp = c.get(path)
        assert resp.status_code == 200, path
        assert PRIV not in resp.text, path
    assert c.get("/api/rw").json()["has_private_key"] is True


def test_put_rw_keeps_stored_private_key_on_empty_string(settings, stub_xray):
    """The UI never receives the key, so a plain settings round-trip must not blank it."""
    c = _client(settings, stub_xray)
    h = _auth(c)
    c.put("/api/rw", json={"private_key": PRIV}, headers=h)
    c.put("/api/rw", json={"port": 8443, "private_key": ""}, headers=h)
    body = c.get("/api/rw").json()
    assert body["has_private_key"] is True and body["port"] == 8443


def test_enabling_without_a_private_key_is_rejected(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = _auth(c)
    r = c.put("/api/rw", json={"enabled": True}, headers=h)
    assert r.status_code == 422 and "x25519" in r.json()["detail"]


def test_mutations_require_auth_and_csrf(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = _auth(c)
    for method, path, body in (("put", "/api/rw", {"port": 443}),
                               ("post", "/api/rw/clients", {"email": "x"}),
                               ("delete", "/api/rw/clients/abc", None)):
        r = getattr(c, method)(path, json=body) if body else getattr(c, method)(path)
        assert r.status_code == 403, f"{method} {path} accepted without CSRF"
    c.post("/api/logout", headers=h)
    for path in ("/api/rw", "/api/rw/clients/abc/link", "/api/rw/clients/abc/config"):
        assert c.get(path).status_code == 401, path


def test_client_crud_and_duplicate_rejection(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = _auth(c)
    body = c.post("/api/rw/clients", json={"email": "iphone"}, headers=h).json()
    assert [x["email"] for x in body["clients"]] == ["iphone"]
    cid = body["clients"][0]["id"]
    assert c.post("/api/rw/clients", json={"email": "iphone"}, headers=h).status_code == 422
    assert c.delete(f"/api/rw/clients/{cid}", headers=h).json()["clients"] == []
    assert c.delete(f"/api/rw/clients/{cid}", headers=h).status_code == 404


def test_bad_host_name_is_rejected_by_the_api(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = _auth(c)
    r = c.put("/api/rw", json={"hosts": {"nas.local": "192.168.1.88"}}, headers=h)
    assert r.status_code == 422 and "mDNS" in r.json()["detail"]


def test_routed_nets_are_derived_from_the_net_plan_not_hardcoded(settings, stub_xray):
    """A hardcoded 192.168.1.0/24 starts lying the moment the addressing changes."""
    c = _client(settings, stub_xray)
    h = _auth(c)
    derived = c.get("/api/rw").json()
    assert derived["routed_nets_override"] == ""
    assert "192.168.10.0/24" in derived["routed_nets"]      # the segment, from the plan
    c.put("/api/network", json={"segment_ip": "10.44.7.2",
                                "dhcp_start": "10.44.7.30",
                                "dhcp_end": "10.44.7.200"}, headers=h)
    assert "10.44.7.0/24" in c.get("/api/rw").json()["routed_nets"]


def test_routed_nets_override_wins_and_is_validated(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = _auth(c)
    body = c.put("/api/rw", json={"routed_nets": "192.168.5.0/24"}, headers=h).json()
    assert body["routed_nets"] == ["192.168.5.0/24"]
    assert c.put("/api/rw", json={"routed_nets": "nonsense"}, headers=h).status_code == 422


def test_config_endpoint_returns_a_filename_and_the_conf(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = _auth(c)
    c.put("/api/rw", json={"private_key": PRIV, "public_key": PUB,
                           "endpoint": "home.example.org", "short_ids": "ab12cd34"}, headers=h)
    cid = c.post("/api/rw/clients", json={"email": "iphone"},
                 headers=h).json()["clients"][0]["id"]
    body = c.get(f"/api/rw/clients/{cid}/config").json()
    assert body["filename"] == "iphone.conf"
    assert "[Proxy]" in body["config"] and "FINAL,iphone" in body["config"]


def test_link_and_config_404_on_unknown_client(settings, stub_xray):
    c = _client(settings, stub_xray)
    _auth(c)
    assert c.get("/api/rw/clients/nope/link").status_code == 404
    assert c.get("/api/rw/clients/nope/config").status_code == 404


def test_link_without_endpoint_is_a_422_not_a_500(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = _auth(c)
    cid = c.post("/api/rw/clients", json={"email": "iphone"},
                 headers=h).json()["clients"][0]["id"]
    assert c.get(f"/api/rw/clients/{cid}/link").status_code == 422


# --- audit fixes: arming guards, suspend, bounds, backup --------------------------------

def test_enabling_requires_everything_the_inbound_and_the_client_need(settings, stub_xray):
    """Arming with a key but no short id used to emit `shortIds: []`, and arming with no
    endpoint/public key produced an inbound with nothing issuable to a client. Both are
    deterministic to check here, and miserable to diagnose later."""
    c = _client(settings, stub_xray)
    h = _auth(c)
    c.put("/api/rw", json={"private_key": PRIV}, headers=h)
    for missing, payload in (
            ("short id", {"enabled": True, "public_key": PUB, "endpoint": "e.example.org",
                          "server_names": "www.microsoft.com", "short_ids": ""}),
            ("public key", {"enabled": True, "public_key": "", "endpoint": "e.example.org",
                            "server_names": "www.microsoft.com", "short_ids": "ab12"}),
            ("endpoint", {"enabled": True, "public_key": PUB, "endpoint": "",
                          "server_names": "www.microsoft.com", "short_ids": "ab12"}),
            ("server name", {"enabled": True, "public_key": PUB, "endpoint": "e.example.org",
                             "server_names": "", "short_ids": "ab12"})):
        r = c.put("/api/rw", json=payload, headers=h)
        assert r.status_code == 422, f"arming accepted with no {missing}"
    assert c.get("/api/rw").json()["enabled"] is False


def test_a_malformed_stored_port_reports_itself_instead_of_500ing(settings, stub_xray):
    """Hand-edited DB or a foreign backup. A 500 here would make the one screen that could fix
    the damage unreachable."""
    c = _client(settings, stub_xray)
    _auth(c)
    c.app.state.app_state.store.set_setting("rw_port", "not-a-number")
    body = c.get("/api/rw").json()
    assert body["port"] == 443
    assert "not-a-number" in body["state_error"]


def test_a_malformed_stored_host_map_reports_itself_too(settings, stub_xray):
    c = _client(settings, stub_xray)
    _auth(c)
    c.app.state.app_state.store.set_setting("rw_hosts", json.dumps({"nas.local": "192.168.1.88"}))
    body = c.get("/api/rw").json()
    assert body["hosts"] == {} and "mDNS" in body["state_error"]


def test_suspending_a_client_keeps_its_uuid_but_drops_it_from_the_config(settings, stub_xray):
    """The lost-phone case: revoke access now, keep the identity so nothing is reissued."""
    c = _client(settings, stub_xray)
    h = _auth(c)
    cid = c.post("/api/rw/clients", json={"email": "iphone"},
                 headers=h).json()["clients"][0]["id"]
    body = c.patch(f"/api/rw/clients/{cid}", json={"enabled": False}, headers=h).json()
    assert body["clients"][0]["enabled"] is False and body["clients"][0]["id"] == cid
    body = c.patch(f"/api/rw/clients/{cid}", json={"enabled": True}, headers=h).json()
    assert body["clients"][0]["enabled"] is True
    assert c.patch("/api/rw/clients/nope", json={"enabled": False}, headers=h).status_code == 404
    assert c.patch(f"/api/rw/clients/{cid}", json={"enabled": False}).status_code == 403


def test_suspended_clients_are_excluded_from_the_emitted_inbound():
    s = _enabled_store()
    rw.add_client(s, "macbook")
    rw.set_client_enabled(s, rw.get_clients(s)[0]["id"], False)
    cfg = build_config(_node(), Settings(), rw_inbound=rw.resolve(s))
    inb = next(i for i in cfg["inbounds"] if i["tag"] == "rw-in")
    assert [c["email"] for c in inb["settings"]["clients"]] == ["macbook"]


def test_client_and_host_counts_are_bounded(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = _auth(c)
    for i in range(rw.MAX_CLIENTS):
        assert c.post("/api/rw/clients", json={"email": f"dev{i}"}, headers=h).status_code == 201
    assert c.post("/api/rw/clients", json={"email": "one-too-many"}, headers=h).status_code == 422
    too_many = {f"h{i}.v2pi": "192.168.1.10" for i in range(33)}
    assert c.put("/api/rw", json={"hosts": too_many}, headers=h).status_code == 422


def test_short_id_generator_endpoint(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = _auth(c)
    assert c.post("/api/rw/short-id").status_code == 403          # CSRF
    sid = c.post("/api/rw/short-id", headers=h).json()["short_id"]
    assert rw.validate_short_ids([sid]) == [sid] and len(sid) == 16
    assert c.post("/api/rw/short-id", headers=h).json()["short_id"] != sid   # fresh each time


def test_backup_carries_remote_access_but_never_the_private_key(settings, stub_xray, tmp_path):
    """A restore onto a fresh host must bring remote access back — minus the one secret that
    must not travel in a document the browser downloads."""
    from pi_gw_panel.backup import _SETTINGS_SET, _SETTINGS_NEVER_BACKED_UP
    assert _SETTINGS_NEVER_BACKED_UP.isdisjoint(_SETTINGS_SET)
    assert "rw_private_key" not in _SETTINGS_SET

    c = _client(settings, stub_xray)
    h = _auth(c)
    c.put("/api/rw", json={"private_key": PRIV, "public_key": PUB, "endpoint": "home.example.org",
                           "short_ids": "ab12cd34",
                           "hosts": {"nas.v2pi": "192.168.1.88"}}, headers=h)
    c.post("/api/rw/clients", json={"email": "iphone"}, headers=h)

    doc = c.get("/api/backup").json()
    assert PRIV not in json.dumps(doc)
    assert doc["settings"]["rw_endpoint"] == "home.example.org"
    assert "iphone" in doc["settings"]["rw_clients"]
    assert json.loads(doc["settings"]["rw_hosts"]) == {"nas.v2pi": "192.168.1.88"}

    # Restore onto a genuinely FRESH host (its own data dir + DB) — the migration case, which is
    # the one that used to lose remote access silently. A same-host restore proves nothing here:
    # rw_* aren't in the delete list, so they'd survive by accident.
    other = tmp_path / "host2"
    other.mkdir()
    settings2 = Settings(data_dir=str(other), db_path=str(other / "test.sqlite"),
                         config_path=str(other / "xray.json"),
                         lastgood_path=str(other / "xray.lastgood.json"))
    fresh = _client(settings2, stub_xray)
    h2 = _auth(fresh)
    assert fresh.get("/api/rw").json()["clients"] == []           # nothing there yet
    assert fresh.post("/api/restore", json=doc, headers=h2).status_code == 200
    restored = fresh.get("/api/rw").json()
    assert [x["email"] for x in restored["clients"]] == ["iphone"]
    assert restored["endpoint"] == "home.example.org"
    assert restored["has_private_key"] is False      # re-enter by hand, as documented


# --- controller: a broken stored setting must not keep the tunnel down ------------------

def test_invalid_stored_settings_degrade_to_off_instead_of_blocking_apply():
    """Values are validated on write, so this is restored/hand-edited state. Building must
    still succeed: a malformed remote-access setting cannot be allowed to hold the tunnel
    down on every reconnect."""
    from pi_gw_panel.controller import build_node_config
    s = _enabled_store(rw_short_ids="not-hex")
    cfg = build_node_config(_node(), Settings(), s)
    assert not any(i["tag"] == "rw-in" for i in cfg["inbounds"])
    assert any(i["tag"] == "tproxy-in" for i in cfg["inbounds"])     # tunnel still built
