"""Road-warrior inbound: config emission, client artifacts, and the boundaries that matter.

The load-bearing invariants here are negative ones — what must NOT happen: the Reality private
key must never reach the API, an empty client list must never emit an inbound (xray won't start),
the generated Shadowrocket .conf must never bypass private ranges, and with the feature off the
config must be byte-identical to what ships today.
"""
import json
import os
import threading

import pytest
from fastapi.testclient import TestClient

from pi_gw_panel.config import Settings
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.models import Node
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.xray_config.builder import build_config
from pi_gw_panel import rw_inbound as rw
from conftest import _client, _login

# Shaped like real `xray x25519` output — 32 raw bytes, base64url, 43 chars. Not a real
# keypair, but the API now refuses anything that isn't an x25519 key, so a "looks-like-base64"
# placeholder no longer stands in for one.
PRIV = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
PUB = "ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8"


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
    # requirement 3 rests on these rules and their order; nothing here may touch them. The
    # loopback guard leads: client-facing inbounds may never reach the gateway's own
    # 127.0.0.1 services, and only then does private→direct carry the LAN.
    assert base["routing"]["rules"] == [
        {"type": "field", "inboundTag": ["tproxy-in"],
         "ip": ["0.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "::/128", "::1/128", "fe80::/10"],
         "outboundTag": "block"},
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
    assert cfg["routing"]["rules"][0]["outboundTag"] == "block"      # loopback guard leads
    assert cfg["routing"]["rules"][1] == {"type": "field", "ip": ["geoip:private"],
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
    # exact names only — a `domain:<last label>` suffix rule would drag every sibling domain
    # (for `nas.example.com`: all of .com) onto the plain freedom outbound
    assert rules[-2] == {"type": "field", "domain": ["full:nas.v2pi", "full:pve.v2pi"],
                         "outboundTag": "direct-lan"}
    assert rules[-1]["network"] == "tcp,udp"        # catch-all is still last


def test_local_suffix_is_rejected():
    """iOS/macOS answer .local over mDNS and never hand it to the proxy — accepting it would
    ship a config that silently never works."""
    with pytest.raises(ValueError, match="mDNS"):
        rw.validate_hosts({"nas.local": "192.168.1.88"})


def test_a_name_under_a_real_public_suffix_is_accepted_and_captures_only_itself():
    """Split-horizon is a legitimate thing to want: an operator who owns `example.com` may map
    `nas.example.com` to a LAN address. A hand-kept TLD list used to refuse it — and with it
    every ccTLD, i.e. every operator outside the gTLD space — while still accepting the many
    delegated gTLDs the list had never heard of. What actually made a public name dangerous was
    a suffix rule capturing everything else under it, and that is now structural: one exact
    `full:` rule per mapping."""
    for name in ("nas.example.com", "nas.corp.ru", "nas.v2pi", "nas.lan"):
        assert rw.validate_hosts({name: "192.168.1.88"}) == {name: "192.168.1.88"}
    s = _enabled_store()
    s.set_setting("rw_hosts", json.dumps({"nas.example.com": "192.168.1.88"}))
    cfg = build_config(_node(), Settings(), rw_inbound=rw.resolve(s))
    lan = next(r for r in cfg["routing"]["rules"] if r.get("outboundTag") == "direct-lan")
    assert lan["domain"] == ["full:nas.example.com"]
    assert not any(d.startswith("domain:") for r in cfg["routing"]["rules"]
                   for d in (r.get("domain") or []))


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
    _login(c)
    body = c.get("/api/rw").json()
    assert body["enabled"] is False and body["port"] == 443
    assert body["has_private_key"] is False and body["clients"] == []
    assert body["live"] is False            # xray is down, so nothing is serving an inbound


def test_private_key_never_appears_in_any_response(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
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
    h = {"X-CSRF-Token": _login(c)}
    c.put("/api/rw", json={"private_key": PRIV}, headers=h)
    c.put("/api/rw", json={"port": 8443, "private_key": ""}, headers=h)
    body = c.get("/api/rw").json()
    assert body["has_private_key"] is True and body["port"] == 8443


def test_enabling_without_a_private_key_is_rejected(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    r = c.put("/api/rw", json={"enabled": True}, headers=h)
    assert r.status_code == 422 and "x25519" in r.json()["detail"]


def test_mutations_require_auth_and_csrf(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
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
    h = {"X-CSRF-Token": _login(c)}
    body = c.post("/api/rw/clients", json={"email": "iphone"}, headers=h).json()
    assert [x["email"] for x in body["clients"]] == ["iphone"]
    cid = body["clients"][0]["id"]
    assert c.post("/api/rw/clients", json={"email": "iphone"}, headers=h).status_code == 422
    assert c.delete(f"/api/rw/clients/{cid}", headers=h).json()["clients"] == []
    assert c.delete(f"/api/rw/clients/{cid}", headers=h).status_code == 404


def test_bad_host_name_is_rejected_by_the_api(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    r = c.put("/api/rw", json={"hosts": {"nas.local": "192.168.1.88"}}, headers=h)
    assert r.status_code == 422 and "mDNS" in r.json()["detail"]


def test_routed_nets_are_derived_from_the_net_plan_not_hardcoded(settings, stub_xray):
    """A hardcoded 192.168.1.0/24 starts lying the moment the addressing changes."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    derived = c.get("/api/rw").json()
    assert derived["routed_nets_override"] == ""
    assert "192.168.10.0/24" in derived["routed_nets"]      # the segment, from the plan
    c.put("/api/network", json={"segment_ip": "10.44.7.2",
                                "dhcp_start": "10.44.7.30",
                                "dhcp_end": "10.44.7.200"}, headers=h)
    assert "10.44.7.0/24" in c.get("/api/rw").json()["routed_nets"]


def test_routed_nets_override_wins_and_is_validated(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    body = c.put("/api/rw", json={"routed_nets": "192.168.5.0/24"}, headers=h).json()
    assert body["routed_nets"] == ["192.168.5.0/24"]
    assert c.put("/api/rw", json={"routed_nets": "nonsense"}, headers=h).status_code == 422


def test_config_endpoint_returns_a_filename_and_the_conf(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    c.put("/api/rw", json={"private_key": PRIV, "public_key": PUB,
                           "endpoint": "home.example.org", "short_ids": "ab12cd34"}, headers=h)
    cid = c.post("/api/rw/clients", json={"email": "iphone"},
                 headers=h).json()["clients"][0]["id"]
    body = c.get(f"/api/rw/clients/{cid}/config").json()
    assert body["filename"] == "iphone.conf"
    assert "[Proxy]" in body["config"] and "FINAL,iphone" in body["config"]


def test_link_and_config_404_on_unknown_client(settings, stub_xray):
    c = _client(settings, stub_xray)
    _login(c)
    assert c.get("/api/rw/clients/nope/link").status_code == 404
    assert c.get("/api/rw/clients/nope/config").status_code == 404


def test_link_without_endpoint_is_a_422_not_a_500(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    cid = c.post("/api/rw/clients", json={"email": "iphone"},
                 headers=h).json()["clients"][0]["id"]
    assert c.get(f"/api/rw/clients/{cid}/link").status_code == 422


# --- audit fixes: arming guards, suspend, bounds, backup --------------------------------

def test_enabling_requires_everything_the_inbound_and_the_client_need(settings, stub_xray):
    """Arming with a key but no short id used to emit `shortIds: []`, and arming with no
    endpoint/public key produced an inbound with nothing issuable to a client. Both are
    deterministic to check here, and miserable to diagnose later."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
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
    _login(c)
    c.app.state.app_state.store.set_setting("rw_port", "not-a-number")
    body = c.get("/api/rw").json()
    assert body["port"] == 443
    assert "not-a-number" in body["state_error"]


def test_a_malformed_stored_host_map_reports_itself_too(settings, stub_xray):
    c = _client(settings, stub_xray)
    _login(c)
    c.app.state.app_state.store.set_setting("rw_hosts", json.dumps({"nas.local": "192.168.1.88"}))
    body = c.get("/api/rw").json()
    assert body["hosts"] == {} and "mDNS" in body["state_error"]


def test_suspending_a_client_keeps_its_uuid_but_drops_it_from_the_config(settings, stub_xray):
    """The lost-phone case: revoke access now, keep the identity so nothing is reissued."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
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
    h = {"X-CSRF-Token": _login(c)}
    for i in range(rw.MAX_CLIENTS):
        assert c.post("/api/rw/clients", json={"email": f"dev{i}"}, headers=h).status_code == 201
    assert c.post("/api/rw/clients", json={"email": "one-too-many"}, headers=h).status_code == 422
    too_many = {f"h{i}.v2pi": "192.168.1.10" for i in range(33)}
    assert c.put("/api/rw", json={"hosts": too_many}, headers=h).status_code == 422


def test_short_id_generator_endpoint(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
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
    h = {"X-CSRF-Token": _login(c)}
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
    h2 = {"X-CSRF-Token": _login(fresh)}
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


# --- revocation must reach the LIVE inbound, or say it could not -------------------------

def _live_client_ids(settings) -> list[str]:
    """The uuids the config xray is actually running on will accept."""
    with open(settings.config_path) as f:
        cfg = json.load(f)
    return [cl["id"] for i in cfg.get("inbounds", []) if i.get("tag") == "rw-in"
            for cl in i["settings"]["clients"]]


def _add_node(c, h) -> int:
    return c.post("/api/nodes", json={"name": "n1", "address": "1.2.3.4", "port": 47000,
                                      "uuid": "u-1", "sni": "www.microsoft.com",
                                      "public_key": "PK", "short_id": "ab12"},
                  headers=h).json()["id"]


RW_ARMED = {"enabled": True, "public_key": PUB, "endpoint": "home.example.org",
            "short_ids": "ab12cd34", "server_names": "www.microsoft.com",
            "dest": "www.microsoft.com:443"}


def _armed_with_two_clients(c, h) -> tuple[int, str, str]:
    """A node applied and two remote clients live in the running config."""
    nid = _add_node(c, h)
    c.put("/api/rw", json={**RW_ARMED, "private_key": PRIV}, headers=h)
    lost = c.post("/api/rw/clients", json={"email": "lost-phone"},
                  headers=h).json()["clients"][0]["id"]
    kept = next(x["id"] for x in c.post("/api/rw/clients", json={"email": "laptop"},
                                        headers=h).json()["clients"] if x["email"] == "laptop")
    assert c.post(f"/api/nodes/{nid}/apply", headers=h).status_code == 200
    assert sorted(_live_client_ids(c.app.state.app_state.settings)) == sorted([lost, kept])
    return nid, lost, kept


def test_revoking_with_no_active_node_rebuilds_the_live_inbound(settings, stub_xray):
    """H4: `disconnect` clears the active node but leaves xray RUNNING on the old config, so the
    normal reapply has nothing to rebuild and returned without touching anything — the revoked
    device kept LAN + tunnel access until some unrelated rebuild happened. Revocation is the
    lost-device path; it must never be a silent no-op."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)

    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    assert c.get("/api/status").json()["active_node_id"] is None
    assert c.get("/api/status").json()["running"] is True        # xray deliberately left up

    body = c.delete(f"/api/rw/clients/{lost}", headers=h).json()
    assert body["revocation"] == "rebuilt"        # the response says HOW it was applied
    live = _live_client_ids(settings)
    assert lost not in live, "revoked uuid still accepted by the running inbound"
    assert kept in live                            # the other device is untouched
    assert c.get("/api/status").json()["running"] is True


def test_suspending_with_no_active_node_also_reaches_the_live_inbound(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)

    body = c.patch(f"/api/rw/clients/{lost}", json={"enabled": False}, headers=h).json()
    assert body["revocation"] == "rebuilt"
    assert _live_client_ids(settings) == [kept]
    # resuming only grants access, so it may wait for the next apply
    assert c.patch(f"/api/rw/clients/{lost}", json={"enabled": True}, headers=h).json()[
        "revocation"] == ""


def test_a_revocation_with_no_node_left_strips_the_client_from_the_config_it_has(settings,
                                                                                stub_xray):
    """Nothing left to REBUILD from — the node was deleted after disconnecting, the ordinary
    aftermath of losing a device — and xray is still serving the inbound.

    Rebuilding from a node is a whole-config render, so it needs one; REMOVING a credential from
    the config already on disk needs nothing. Requiring the node made this state a revocation
    that could not clean the file at all: it stopped xray (which the next `/xray/start` undoes)
    and left the uuid in the very file that start comes up on. Sanitizing the live config cuts
    the lost device off for good and costs the other device nothing — the outage the old stop
    caused was never the price of revoking, only of insisting on a rebuild.
    """
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    assert c.delete(f"/api/nodes/{nid}", headers=h).status_code == 200

    body = c.delete(f"/api/rw/clients/{lost}", headers=h).json()
    assert body["revocation"] == "rebuilt"
    assert _live_client_ids(settings) == [kept], "the revoked uuid survived in the live config"
    assert c.get("/api/status").json()["running"] is True    # no self-inflicted outage


def test_a_revocation_with_no_node_left_survives_the_next_bare_start(settings, stub_xray):
    """The exploit, through the door a missing node opens. Deleted node AND xray deliberately
    down: the revocation has nothing to rebuild from and nothing to reload, so the file was left
    untouched — and `/xray/start` with no active node is a bare supervisor.start() on that exact
    file, which handed the lost device its access straight back."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    c.delete(f"/api/nodes/{nid}", headers=h)
    c.post("/api/xray/stop", headers=h)
    assert lost in _live_client_ids(settings)            # the file still names the lost device

    body = c.delete(f"/api/rw/clients/{lost}", headers=h).json()
    assert body["revocation"] == "not-live"
    assert c.get("/api/status").json()["running"] is False, \
        "a revocation started an xray the operator had deliberately stopped"
    assert lost not in _live_client_ids(settings)

    c.post("/api/xray/start", headers=h)                 # the bare-start path, no active node
    assert c.get("/api/status").json()["running"] is True
    assert _live_client_ids(settings) == [kept], "the revoked uuid came back on the next start"


def test_a_revocation_whose_first_write_is_rejected_still_cleans_the_config(settings, stub_xray,
                                                                           monkeypatch):
    """The write itself failing is the other half of the same hole. One rejected write — an
    `xray -test` that refuses the result, a build that raises — used to end the revocation at
    the stop with the credential still in the file, and `/xray/start` undid the stop.

    There are two independent ways to produce a clean config and they fail for different
    reasons: sanitizing needs the live file to be readable, rebuilding needs a node. A write
    that does not land must therefore try the other one before giving up.

    Patches `apply_irreversible`, which is the writer a revocation goes through: the ordinary
    `apply` files the config it replaces as a promotable rollback target, which for a revocation
    is a pre-revocation config the operator can reinstate with one button.
    """
    from pi_gw_panel.xray_config.validate import ConfigManager

    calls: list[int] = []
    real_apply = ConfigManager.apply_irreversible

    def _rejects_the_first(self, cfg):
        calls.append(1)
        if len(calls) == 1:
            return False, "config error: the first candidate was rejected"
        return real_apply(self, cfg)

    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)

    monkeypatch.setattr("pi_gw_panel.api.routes.ConfigManager.apply_irreversible",
                        _rejects_the_first)
    body = c.delete(f"/api/rw/clients/{lost}", headers=h).json()
    monkeypatch.undo()

    assert len(calls) == 2, "a rejected write ended the revocation instead of trying the other way"
    assert body["revocation"] == "rebuilt"
    assert _live_client_ids(settings) == [kept], "the revoked uuid survived a rejected write"

    c.post("/api/xray/start", headers=h)
    assert _live_client_ids(settings) == [kept], "the revoked uuid came back on the next start"


def test_turning_the_feature_off_is_treated_as_a_revocation(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)

    body = c.put("/api/rw", json={**RW_ARMED, "enabled": False}, headers=h).json()
    assert body["revocation"] == "rebuilt"
    assert _live_client_ids(settings) == []          # the whole inbound is gone
    assert lost and kept                             # both were revoked, not just one


def test_a_plain_settings_save_is_not_a_revocation(settings, stub_xray):
    """Only revocations take the fail-safe path — a normal save must not be able to stop xray."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    _armed_with_two_clients(c, h)
    body = c.put("/api/rw", json={**RW_ARMED, "hosts": {"nas.v2pi": "192.168.1.88"}},
                 headers=h).json()
    assert body["revocation"] == "" and body["live"] is True
    assert c.get("/api/status").json()["running"] is True


def test_turning_off_an_inbound_that_was_never_live_does_not_stop_xray(settings, stub_xray):
    """The fail-safe path is for cutting LIVE access. Arming with no clients emits no inbound at
    all, so switching the feature back off revokes nothing — and must not take xray down to
    prove it, even with nothing left to rebuild from."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid = _add_node(c, h)
    c.put("/api/rw", json={**RW_ARMED, "private_key": PRIV}, headers=h)   # armed, zero clients
    c.post(f"/api/nodes/{nid}/apply", headers=h)
    assert not any(i["tag"] == "rw-in" for i in json.load(open(settings.config_path))["inbounds"])
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    c.delete(f"/api/nodes/{nid}", headers=h)                              # nothing to rebuild from

    body = c.put("/api/rw", json={**RW_ARMED, "enabled": False}, headers=h).json()
    assert body["revocation"] == "not-live"
    assert c.get("/api/status").json()["running"] is True


def _live_inbound(settings) -> dict:
    """The `rw-in` inbound of the config xray is actually running on ({} when there is none)."""
    with open(settings.config_path) as f:
        cfg = json.load(f)
    return next((i for i in cfg.get("inbounds", []) if i.get("tag") == "rw-in"), {})


def _live_reality(settings) -> dict:
    return _live_inbound(settings)["streamSettings"]["realitySettings"]


def test_rotating_a_short_id_with_no_active_node_cuts_the_old_one(settings, stub_xray):
    """The most natural remediation an operator reaches for after losing a device — rotate the
    short id — was classified as a widening change, so with no active node it was stored and
    "picked up on the next connect" while the running inbound kept accepting the old one. Every
    credential that can be narrowed has to take the same fail-safe path the key rotation does."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, _lost, _kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    assert _live_reality(settings)["shortIds"] == ["ab12cd34"]

    body = c.put("/api/rw", json={**RW_ARMED, "short_ids": "99998888"}, headers=h).json()
    assert body["revocation"] == "rebuilt"
    assert _live_reality(settings)["shortIds"] == ["99998888"], \
        "the running inbound still accepts the rotated-away short id"


def test_dropping_a_server_name_or_moving_the_port_is_a_revocation_too(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, _lost, _kept = _armed_with_two_clients(c, h)
    two_names = {**RW_ARMED, "server_names": "www.microsoft.com,www.bing.com"}
    assert c.put("/api/rw", json=two_names, headers=h).json()["revocation"] == ""   # widening
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)

    body = c.put("/api/rw", json=RW_ARMED, headers=h).json()      # back to one name
    assert body["revocation"] == "rebuilt"
    assert _live_reality(settings)["serverNames"] == ["www.microsoft.com"]

    body = c.put("/api/rw", json={**RW_ARMED, "port": 8443}, headers=h).json()
    assert body["revocation"] == "rebuilt"
    assert _live_inbound(settings)["port"] == 8443    # nothing is left listening on 443


def test_widening_the_credentials_is_never_treated_as_a_revocation(settings, stub_xray):
    """The fail-safe path can stop xray outright, so over-classifying is not free: adding a
    short id or a server name only grants access and must take the ordinary path."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, _lost, _kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    c.delete(f"/api/nodes/{nid}", headers=h)          # nothing left to rebuild from

    body = c.put("/api/rw", json={**RW_ARMED, "short_ids": "ab12cd34,99998888",
                                  "server_names": "www.microsoft.com,www.bing.com"},
                 headers=h).json()
    assert body["revocation"] == ""
    assert c.get("/api/status").json()["running"] is True
    # unchanged credentials in a different letter case are the same credentials, not a rotation
    assert c.put("/api/rw", json={**RW_ARMED, "short_ids": "AB12CD34,99998888",
                                 "server_names": "www.microsoft.com,www.bing.com"},
                 headers=h).json()["revocation"] == ""


def test_live_reports_what_is_being_served_not_whether_a_node_is_active(settings, stub_xray):
    """`disconnect` clears the active node and leaves xray running on the config it already
    loaded, so the inbound keeps serving. Deriving `live` from active_node_id reported that as
    "not in the running config yet" — and reported a successful revocation rebuild the same way,
    on the one screen an operator checks to decide whether a lost device can still get in."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    assert c.get("/api/rw").json()["live"] is True

    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    assert c.get("/api/status").json()["active_node_id"] is None
    assert c.get("/api/rw").json()["live"] is True, "the inbound is still serving clients"

    # a revocation that rebuilds leaves everyone who was not revoked served
    body = c.delete(f"/api/rw/clients/{lost}", headers=h).json()
    assert body["revocation"] == "rebuilt" and body["live"] is True
    assert _live_client_ids(settings) == [kept]

    # ...and once the last client goes, no inbound is emitted at all
    body = c.delete(f"/api/rw/clients/{kept}", headers=h).json()
    assert body["live"] is False and _live_inbound(settings) == {}


def test_a_missing_live_config_is_never_proof_that_nothing_is_serving(settings, stub_xray):
    """xray keeps serving the configuration it already loaded long after the file is unlinked,
    so a missing config is evidence about the filesystem, not about what is listening. Reading
    it as "nothing is live" made every revocation answer `not-live` and return having done
    nothing — neither rebuilding nor stopping — while the client kept its access. A missing
    config has to fail safe exactly like an unreadable one; only a stopped supervisor is proof.
    """
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    os.unlink(settings.config_path)                      # xray is still serving what it loaded
    assert c.get("/api/status").json()["running"] is True
    assert c.get("/api/rw").json()["live"] is True, "we cannot prove nothing is listening"

    body = c.delete(f"/api/rw/clients/{lost}", headers=h).json()
    assert body["revocation"] == "rebuilt", "the revocation silently no-opped"
    assert _live_client_ids(settings) == [kept]

    # ...and with nothing left to rebuild from it stops xray rather than no-op, same as always
    c.delete(f"/api/nodes/{nid}", headers=h)
    os.unlink(settings.config_path)
    body = c.delete(f"/api/rw/clients/{kept}", headers=h).json()
    assert body["revocation"] == "stopped"
    assert c.get("/api/status").json()["running"] is False


def _unqueryable_status():
    raise OSError("cannot query the supervised process")


def test_a_supervisor_that_cannot_be_queried_is_never_proof_that_nothing_is_serving(settings,
                                                                                    stub_xray):
    """The same rule, reached through a third door: a supervisor whose status raises tells us
    that we failed to observe, not that nothing is listening. Reading the exception as "not
    running" made `live` report False and every revocation answer `not-live` and return having
    neither rebuilt nor stopped anything — precisely when the operator has least reason to
    trust the box. Only a supervisor that affirmatively reports not-running is proof.

    Unknown is not proof of PRESENCE either, so the action it earns is the one that is right
    under both readings — stop — never a rebuild: reload() is stop→start, and starting an xray
    that may have been deliberately stopped is the opposite of revoking (see the next test)."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, _kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)

    sup = c.app.state.app_state.supervisor
    real_status = sup.status
    sup.status = _unqueryable_status
    try:
        assert c.get("/api/rw").json()["live"] is True, "we cannot prove nothing is listening"

        body = c.delete(f"/api/rw/clients/{lost}", headers=h).json()
        assert body["revocation"] == "stopped", "the revocation silently no-opped"
    finally:
        sup.status = real_status
    assert c.get("/api/status").json()["running"] is False


def test_an_unknown_supervisor_state_never_starts_an_xray_that_was_stopped(settings, stub_xray):
    """Reporting an unqueryable supervisor as "serving" is right; ACTING on it as if xray were
    running is not. The rebuild branch reloads, and reload() is an unconditional stop→start —
    so with xray genuinely stopped (the operator took it down; /xray/stop keeps the selection)
    and a previous node still on file, a revocation would START xray and hand the remaining
    clients an inbound nobody was serving. A revocation may take access away, never give it."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)     # xray stays up, prev node recorded
    c.post("/api/xray/stop", headers=h)                   # ...and now it is deliberately down
    assert c.get("/api/status").json()["running"] is False
    assert kept in _live_client_ids(settings)             # the config still carries the inbound

    sup = c.app.state.app_state.supervisor
    real_status = sup.status
    sup.status = _unqueryable_status                      # ...and the state becomes unknowable
    try:
        body = c.delete(f"/api/rw/clients/{lost}", headers=h).json()
        assert body["revocation"] == "stopped", "an unknown state must not take the reload path"
    finally:
        sup.status = real_status
    assert c.get("/api/status").json()["running"] is False, \
        "a revocation started an xray the operator had deliberately stopped"


def test_a_supervisor_that_throws_on_reload_still_fails_safe(settings, stub_xray):
    """A rebuild that cannot be confirmed is a failed rebuild. If the reload itself raises, the
    revocation must fall through to the stop path, not escape as a 500 leaving the old config —
    with the revoked client in it — being served."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, _kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)

    sup = c.app.state.app_state.supervisor
    real_reload = sup.reload

    def _broken_reload():
        raise OSError("cannot restart the supervised process")

    sup.reload = _broken_reload
    try:
        body = c.delete(f"/api/rw/clients/{lost}", headers=h).json()
        assert body["revocation"] == "stopped"
    finally:
        sup.reload = real_reload
    assert c.get("/api/status").json()["running"] is False


def test_every_config_shape_that_cannot_prove_absence_counts_as_serving(tmp_path):
    """The fourth door: the fail-safe covered opening and parsing the live config but not the
    STRUCTURE it parsed. A syntactically valid value of the wrong shape — `[]`, a bare string,
    an `inbounds` that is not a list of objects — raised straight out of the helper (`[]` →
    AttributeError), so the revocation returned 500 having neither rebuilt nor stopped
    anything: the same silent no-op as a missing file, arriving through malformed content.
    Only a well-formed inbounds list without an `rw-in` tag proves nothing is being served."""
    from types import SimpleNamespace
    from pi_gw_panel.api import routes

    path = tmp_path / "xray.json"
    state = SimpleNamespace(settings=SimpleNamespace(config_path=str(path)))
    for junk in ("[]", "null", "3", '"nope"', "{}", '{"inbounds": null}', '{"inbounds": 3}',
                 '{"inbounds": "rw-in"}', '{"inbounds": {"rw-in": {}}}', '{"inbounds": [null]}',
                 '{"inbounds": ["rw-in"]}', "{not json at all"):
        path.write_text(junk)
        assert routes._rw_live_inbound(state) is True, f"{junk} was read as proof of absence"

    path.write_text(json.dumps({"inbounds": [{"tag": "tproxy-in"}]}))
    assert routes._rw_live_inbound(state) is False        # positively proven to lack rw-in
    path.write_text(json.dumps({"inbounds": [{"tag": "tproxy-in"}, {"tag": "rw-in"}]}))
    assert routes._rw_live_inbound(state) is True


def test_a_malformed_live_config_is_never_proof_that_nothing_is_serving(settings, stub_xray):
    """The same door from the outside: a live config that parses but has the wrong shape must
    take the fail-safe path end to end — `live` still True, and the revocation still reaching
    the running inbound instead of raising out of the request."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    with open(settings.config_path, "w") as f:
        f.write("[]")                       # valid JSON, not an xray config
    assert c.get("/api/rw").json()["live"] is True, "we cannot prove nothing is listening"

    body = c.delete(f"/api/rw/clients/{lost}", headers=h).json()
    assert body["revocation"] == "rebuilt", "the revocation silently no-opped"
    assert _live_client_ids(settings) == [kept]


# --- a revocation must survive the next start, and must never BE a start -----------------
#
# Two halves of one interaction. The `stopped`/`not-live` branches never rewrote the ON-DISK
# config, so a revoked client stayed listed in the file — and `/xray/start` with no active node
# is a bare supervisor.start() on that exact file, which handed the credential straight back.
# Meanwhile `/xray/stop` keeps the node selection, so a revocation issued while xray was
# deliberately down went through reapply_active_node and STARTED it. Writing the config and
# making the process pick it up are separate actions: the write is safe in every supervisor
# state, the reload is safe in exactly one.


def test_a_revocation_while_xray_is_stopped_cleans_the_config_without_starting_it(settings,
                                                                                 stub_xray):
    """`/xray/stop` keeps the active node, so the revocation used to enter reapply_active_node
    and bring the whole tunnel back up — xray started, net rules re-applied — in the name of
    taking access away. A revocation may never give any back."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    c.post("/api/xray/stop", headers=h)                   # deliberately down, selection KEPT
    assert c.get("/api/status").json()["active_node_id"] == nid
    assert c.get("/api/status").json()["running"] is False
    assert lost in _live_client_ids(settings)             # the file still names the lost device

    body = c.delete(f"/api/rw/clients/{lost}", headers=h).json()
    assert c.get("/api/status").json()["running"] is False, \
        "a revocation started an xray the operator had deliberately stopped"
    assert lost not in _live_client_ids(settings), \
        "the revoked uuid is still in the config xray would come up on"
    assert kept in _live_client_ids(settings)             # the other device is untouched
    assert body["revocation"] == "not-live"


def test_a_revocation_while_stopped_survives_the_next_start(settings, stub_xray):
    """The exploit end to end. Disconnected AND stopped, `/xray/start` has no active node to
    rebuild from and falls through to a bare supervisor.start() on whatever the file says — so
    a revocation that left the file alone was undone by the very next start."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)     # prev node recorded, xray still up
    c.post("/api/xray/stop", headers=h)                   # ...and now deliberately down
    assert c.get("/api/status").json()["active_node_id"] is None

    body = c.delete(f"/api/rw/clients/{lost}", headers=h).json()
    assert body["revocation"] == "not-live"
    assert c.get("/api/status").json()["running"] is False

    c.post("/api/xray/start", headers=h)                  # the bare-start path, no active node
    assert c.get("/api/status").json()["running"] is True
    assert lost not in _live_client_ids(settings), \
        "the revoked uuid came back on the next start"
    assert kept in _live_client_ids(settings)


def test_an_unknown_supervisor_state_cleans_the_config_it_must_not_start(settings, stub_xray):
    """Unknown still earns a stop and never a start (that half already held), but the stop
    branch left the file carrying the revoked client too — so the credential survived in
    exactly the state where the operator can trust the box least."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    c.post("/api/xray/stop", headers=h)

    sup = c.app.state.app_state.supervisor
    real_status = sup.status
    sup.status = _unqueryable_status
    try:
        body = c.delete(f"/api/rw/clients/{lost}", headers=h).json()
        assert body["revocation"] == "stopped"
    finally:
        sup.status = real_status
    assert c.get("/api/status").json()["running"] is False, \
        "a revocation started an xray whose state it could not even observe"
    assert lost not in _live_client_ids(settings), \
        "the revoked uuid is still in the config xray would come up on"

    c.post("/api/xray/start", headers=h)                  # ...and the start stays clean
    assert c.get("/api/status").json()["running"] is True
    assert _live_client_ids(settings) == [kept]


def test_a_revocation_with_xray_running_still_rebuilds_and_reloads(settings, stub_xray):
    """The working path, pinned. Gating the reconnect on a KNOWN-running supervisor must not
    cost the case it was written for: xray up with a node connected still reapplies, and xray
    up with no active node still rebuilds + reloads."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    assert c.get("/api/status").json()["running"] is True

    body = c.delete(f"/api/rw/clients/{lost}", headers=h).json()
    assert body["revocation"] == "reapplied"              # connected: the normal reapply
    assert _live_client_ids(settings) == [kept]
    assert c.get("/api/status").json()["running"] is True
    assert c.get("/api/status").json()["active_node_id"] == nid

    third = next(x["id"] for x in c.post("/api/rw/clients", json={"email": "tablet"},
                                         headers=h).json()["clients"] if x["email"] == "tablet")
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)     # xray stays up, no active node
    body = c.delete(f"/api/rw/clients/{third}", headers=h).json()
    assert body["revocation"] == "rebuilt"                # disconnected: rebuild + reload
    assert _live_client_ids(settings) == [kept]
    assert c.get("/api/status").json()["running"] is True


@pytest.mark.parametrize("supervisor_state,expected", [
    ("running", "stopped xray to apply a remote-access revocation"),
    ("stopped", "ensured xray remained stopped to apply a remote-access revocation"),
    ("unknown", "issued a stop to an xray whose state could not be observed "
                "to apply a remote-access revocation"),
])
def test_the_revocation_event_records_what_actually_happened(settings, stub_xray,
                                                             supervisor_state, expected):
    """The connection log is what an operator reads afterwards to work out what the box did
    with a lost device, so "stopped xray" has to mean xray was stopped. On an already-down
    supervisor `stop()` changes nothing, and recording it as an action taken writes a state
    transition into the incident record that never happened — while quietly implying the
    revocation was enforced by the stop rather than left unwritten. The same for a supervisor
    whose state could not be observed at all: the record may not claim the observation."""
    from pi_gw_panel.net_control import events as conn_events

    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, _kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    c.delete(f"/api/nodes/{nid}", headers=h)            # nothing left to rebuild from
    if supervisor_state != "running":
        c.post("/api/xray/stop", headers=h)
    os.unlink(settings.config_path)                     # ...and nothing left to sanitize either

    sup = c.app.state.app_state.supervisor
    real_status = sup.status
    if supervisor_state == "unknown":
        sup.status = _unqueryable_status
    try:
        assert c.delete(f"/api/rw/clients/{lost}", headers=h).json()["revocation"] == "stopped"
    finally:
        sup.status = real_status

    store = c.app.state.app_state.store
    details = [e["detail"] for e in conn_events.recent(store) if e["kind"] == "rw-revoke"]
    assert details[-1] == expected


# --- a rollback may never undo a revocation ----------------------------------------------
#
# The door the separated write opened. ConfigManager.apply() files the config it REPLACES as
# the rollback target, so writing a clean config necessarily snapshots the credential-bearing
# one and marks it valid — and `POST /rollback` writes that file back and, with a previous node
# on file, reloads. For an xray the revocation had stopped, reload() is a start. One button,
# both halves of the leak at once: the lost device's uuid live again, on a process the operator
# was told had been taken down. A revocation therefore leaves no rollback target behind.


@pytest.mark.parametrize("supervisor_state", ["running", "stopped", "unknown"])
def test_a_rollback_can_never_undo_a_revocation(settings, stub_xray, supervisor_state):
    """All three supervisor states, because all three reach a write and each undoes differently:
    running rolls the uuid back under a live process, stopped and unknown roll it back AND start
    the process to serve it."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)      # prev_active_node_id now on file
    if supervisor_state != "running":
        c.post("/api/xray/stop", headers=h)

    sup = c.app.state.app_state.supervisor
    real_status = sup.status
    if supervisor_state == "unknown":
        sup.status = _unqueryable_status
    try:
        c.delete(f"/api/rw/clients/{lost}", headers=h)
    finally:
        sup.status = real_status
    running_after_revoke = c.get("/api/status").json()["running"]
    assert lost not in _live_client_ids(settings)          # the revocation itself landed

    r = c.post("/api/rollback", headers=h)
    assert r.status_code == 200
    assert r.json()["ok"] is False, \
        "a revocation left behind a rollback target that reinstates the revoked credential"
    assert lost not in _live_client_ids(settings), "a rollback resurrected the revoked uuid"
    assert kept in _live_client_ids(settings)              # ...and took nothing else away
    assert c.get("/api/status").json()["running"] is running_after_revoke, \
        "a rollback started an xray the revocation had taken down"


def test_a_rollback_that_has_nothing_to_do_with_a_revocation_still_works(settings, stub_xray):
    """The feature, pinned. Refusing to roll back over a revocation must not become refusing to
    roll back — including on this screen: an ordinary remote-access save that only WIDENS access
    is not a revocation and may not cost the operator the undo for the applies around it."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    a = _add_node(c, h)
    c.put("/api/rw", json={**RW_ARMED, "private_key": PRIV}, headers=h)
    cid = c.post("/api/rw/clients", json={"email": "iphone"},
                 headers=h).json()["clients"][0]["id"]
    b = c.post("/api/nodes", json={"name": "n2", "address": "5.6.7.8", "port": 47001,
                                   "uuid": "u-2", "sni": "www.microsoft.com",
                                   "public_key": "PK", "short_id": "cd34"},
               headers=h).json()["id"]
    c.post(f"/api/nodes/{a}/apply", headers=h)
    assert c.put("/api/rw", json={**RW_ARMED, "short_ids": "ab12cd34,99998888"},
                 headers=h).json()["revocation"] == ""     # widening, not a revocation
    c.post(f"/api/nodes/{b}/apply", headers=h)
    assert c.get("/api/status").json()["active_node_id"] == b

    assert c.post("/api/rollback", headers=h).json()["ok"] is True
    assert c.get("/api/status").json()["active_node_id"] == a
    assert _live_client_ids(settings) == [cid], "the rollback did not restore the previous config"


# --- a start may not serve grants the store no longer makes ------------------------------
#
# The last door on the write side. A revocation that cannot write the config (both producers
# rejected, a full disk, an `xray -test` that refuses the result) ends at the stop — and a stop
# is exactly what the next start undoes. `/xray/start` with no active node has nothing to
# rebuild from, so it is a bare supervisor.start() on whatever the file says. Checking the
# file's remote-access grants against the store THERE covers every way it can end up carrying a
# revoked credential, including the ways not yet found.


def _live_cfg(settings) -> dict:
    with open(settings.config_path) as f:
        return json.load(f)


def _rejects_every_write(self, cfg):
    return False, "config error: rejected"


def test_a_revocation_that_could_not_write_at_all_is_not_undone_by_the_next_start(settings,
                                                                                 stub_xray,
                                                                                 monkeypatch):
    """Both config producers fail. The revocation correctly falls back to stopping xray, but the
    credential is still in the file — and the operator's very next Start hands it back."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)

    monkeypatch.setattr("pi_gw_panel.api.routes.ConfigManager.apply_irreversible",
                        _rejects_every_write)
    body = c.delete(f"/api/rw/clients/{lost}", headers=h).json()
    monkeypatch.undo()

    assert body["revocation"] == "stopped"                 # nothing could be written
    assert lost in _live_client_ids(settings)              # ...so the file still names it
    assert c.get("/api/status").json()["running"] is False

    assert c.post("/api/xray/start", headers=h).status_code == 200
    assert c.get("/api/status").json()["running"] is True
    assert _live_client_ids(settings) == [kept], \
        "a bare start served a credential the store no longer grants"


def test_a_start_refuses_a_config_it_cannot_bring_in_line(settings, stub_xray, monkeypatch):
    """When the file cannot be cleaned either, the start FAILS CLOSED. Not starting costs a
    tunnel the operator can restore; starting hands a lost device its access back."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, _kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)

    monkeypatch.setattr("pi_gw_panel.api.routes.ConfigManager.apply_irreversible",
                        _rejects_every_write)
    assert c.delete(f"/api/rw/clients/{lost}", headers=h).json()["revocation"] == "stopped"
    r = c.post("/api/xray/start", headers=h)

    assert r.status_code == 409 and "revoked" in r.json()["detail"]
    assert c.get("/api/status").json()["running"] is False, \
        "xray started on a config granting access the store had revoked"
    assert lost in _live_client_ids(settings)              # untouched, and never served


def test_a_start_leaves_a_config_that_grants_no_more_than_the_store_alone(settings, stub_xray):
    """The guard is a SUBSET test, not an equality one, and it may not become a second apply.
    A file granting exactly what the store grants is started as-is; one granting LESS (a client
    added while xray was down) is stale in the harmless direction and equally not the start
    path's business to widen."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, _lost, _kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    c.post("/api/xray/stop", headers=h)
    before = _live_cfg(settings)

    added = c.post("/api/rw/clients", json={"email": "tablet"}, headers=h).json()
    tablet = next(x["id"] for x in added["clients"] if x["email"] == "tablet")
    assert c.post("/api/xray/start", headers=h).status_code == 200
    assert c.get("/api/status").json()["running"] is True
    assert _live_cfg(settings) == before, "the start path rewrote a config that granted no excess"
    assert tablet not in _live_client_ids(settings)        # granted on the next apply, not here


# --- a revocation owns more than the inbound ---------------------------------------------
#
# `dns.hosts`, the `direct-lan` outbound and the exact-domain rule naming it are emitted only by
# the remote-access feature. Reconciling the inbound alone left all three live: a host removal
# shipped with a credential rotation reported `rebuilt` while the old mapping kept working, and
# turning the feature off left mapped names routed out a plain freedom outbound — for TPROXY
# clients too, since that rule is not scoped to `rw-in`. Same family as the `.com` suffix leak.

HOSTS = {"nas.example.com": "192.168.1.50"}


def _lan_rules(cfg) -> list[dict]:
    return [r for r in cfg["routing"]["rules"] if r.get("outboundTag") == "direct-lan"]


def _armed_with_hosts(c, h) -> tuple[int, str]:
    """A node applied, one client, and one LAN name mapped — all three fragments live."""
    nid = _add_node(c, h)
    c.put("/api/rw", json={**RW_ARMED, "private_key": PRIV, "hosts": HOSTS}, headers=h)
    cid = c.post("/api/rw/clients", json={"email": "iphone"},
                 headers=h).json()["clients"][0]["id"]
    assert c.post(f"/api/nodes/{nid}/apply", headers=h).status_code == 200
    cfg = _live_cfg(c.app.state.app_state.settings)
    assert cfg["dns"]["hosts"] == HOSTS
    assert [o["tag"] for o in cfg["outbounds"] if o["tag"] == "direct-lan"] == ["direct-lan"]
    assert _lan_rules(cfg) == [{"type": "field", "domain": ["full:nas.example.com"],
                                "outboundTag": "direct-lan"}]
    return nid, cid


def test_a_host_removed_in_the_same_save_as_a_rotation_leaves_no_stale_mapping(settings,
                                                                               stub_xray):
    """The sanitizer's blind spot, reached the way an operator reaches it: after losing a device
    you rotate the short id AND drop the LAN name it could reach. The rotation makes the save a
    revocation, which takes the sanitize path — which reconciled the inbound and left the
    name→address mapping, the outbound and the rule exactly as they were."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, cid = _armed_with_hosts(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)      # no node to rebuild from: sanitize

    body = c.put("/api/rw", json={**RW_ARMED, "short_ids": "99998888", "hosts": {}},
                 headers=h).json()
    assert body["revocation"] == "rebuilt"

    cfg = _live_cfg(settings)
    assert "hosts" not in cfg["dns"], "the removed host mapping is still resolvable"
    assert not [o for o in cfg["outbounds"] if o["tag"] == "direct-lan"]
    assert _lan_rules(cfg) == [], "the removed host is still routed out direct-lan"
    assert _live_client_ids(settings) == [cid]             # the client itself is untouched
    assert cfg["inbounds"][-1]["streamSettings"]["realitySettings"]["shortIds"] == ["99998888"]


def test_a_host_kept_across_a_rotation_keeps_working(settings, stub_xray):
    """Reconciling may not become deleting: the fragments must come out matching the store, and
    a mapping the operator kept is still a mapping. Pinned so the fix above cannot be 'remove
    them always', which would quietly break LAN-by-name on every revocation."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, _cid = _armed_with_hosts(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    moved = {"nas.example.com": "192.168.1.51", "printer.example.com": "192.168.1.60"}

    assert c.put("/api/rw", json={**RW_ARMED, "short_ids": "99998888", "hosts": moved},
                 headers=h).json()["revocation"] == "rebuilt"

    cfg = _live_cfg(settings)
    assert cfg["dns"]["hosts"] == moved                    # the new address, not the old one
    assert [o["tag"] for o in cfg["outbounds"] if o["tag"] == "direct-lan"] == ["direct-lan"]
    assert _lan_rules(cfg) == [{"type": "field",
                                "domain": ["full:nas.example.com", "full:printer.example.com"],
                                "outboundTag": "direct-lan"}]
    # ...and where the builder puts it: ahead of the catch-all, or an exact name never matches.
    assert cfg["routing"]["rules"][-1]["outboundTag"] != "direct-lan"


def test_turning_the_feature_off_leaves_no_lan_routing_behind(settings, stub_xray):
    """The leak this closes. With remote access off the builder emits none of the three, but the
    sanitizer left the outbound and its rule in place — and that rule matches TPROXY traffic as
    well, so every LAN client kept sending the mapped name out an untunnelled freedom outbound
    long after the feature that created it was switched off."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, _cid = _armed_with_hosts(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)

    assert c.put("/api/rw", json={**RW_ARMED, "enabled": False, "hosts": HOSTS},
                 headers=h).json()["revocation"] == "rebuilt"

    cfg = _live_cfg(settings)
    assert not [i for i in cfg["inbounds"] if i["tag"] == "rw-in"]
    assert not [o for o in cfg["outbounds"] if o["tag"] == "direct-lan"], \
        "the LAN outbound outlived the feature that created it"
    assert _lan_rules(cfg) == [], "mapped names are still routed past the tunnel"
    assert "hosts" not in cfg["dns"]
    # every dispatch target the rules name still exists — the result has to pass `xray -test`,
    # and a rule pointing at an outbound that was just removed would not. (`api` is a service
    # tag, not an outbound.)
    tags = {o["tag"] for o in cfg["outbounds"]} | {cfg.get("api", {}).get("tag")}
    assert all(r["outboundTag"] in tags for r in cfg["routing"]["rules"])


# --- the revocation's write may never publish a rollback target ---------------------------


def test_a_revocation_leaves_no_rollback_target_even_if_the_sweep_does_nothing(settings,
                                                                              stub_xray,
                                                                              monkeypatch):
    """The sweep after the revocation used to be the only thing standing between the operator
    and a promotable pre-revocation config — and it swallowed its own errors on the reasoning
    that the marker could not match anyway. After a successful revocation it matches EXACTLY:
    `apply()` had just paired the pre-revocation snapshot with the clean live config.

    So the write path must not create the pairing in the first place. Pinned by neutering the
    sweep entirely — the swallowed-unlink case, exactly — and demanding the rollback still fail.
    """
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)      # the write path, not the reapply

    monkeypatch.setattr("pi_gw_panel.api.routes.ConfigManager.invalidate_rollback",
                        lambda self: True)                 # "unlinked it", in fact did nothing
    body = c.delete(f"/api/rw/clients/{lost}", headers=h).json()
    monkeypatch.undo()

    assert body["revocation"] == "rebuilt"
    assert c.get("/api/status").json()["rollback_available"] is False
    r = c.post("/api/rollback", headers=h)
    assert r.json()["ok"] is False, \
        "the revocation's own write published a promotable pre-revocation rollback target"
    assert lost not in _live_client_ids(settings), "a rollback resurrected the revoked uuid"
    assert kept in _live_client_ids(settings)


def test_a_reapplied_revocation_publishes_no_rollback_target(settings, stub_xray, monkeypatch):
    """The connected case — the one an operator actually hits with a lost phone, since the
    tunnel is normally up. It reaches the config through the ordinary `apply_node`, which files
    the config it REPLACES as the undo and marks the pairing valid: a promotable PRE-revocation
    config, published by the revocation itself. The sweep afterwards was the only thing taking
    it away, and a guard that can only run after the door is open is a guard with a window.

    Pinned the same way the write path is: by neutering the sweep into a lying no-op — exactly
    what a swallowed failure looks like — and demanding the rollback still fail.
    """
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    assert c.get("/api/status").json()["running"] is True       # connected: the reapply path

    monkeypatch.setattr("pi_gw_panel.api.routes.ConfigManager.invalidate_rollback",
                        lambda self: True)                      # "dropped it", in fact did nothing
    body = c.delete(f"/api/rw/clients/{lost}", headers=h).json()
    monkeypatch.undo()

    assert body["revocation"] == "reapplied"
    assert _live_client_ids(settings) == [kept]
    assert c.get("/api/status").json()["rollback_available"] is False
    r = c.post("/api/rollback", headers=h)
    assert r.json()["ok"] is False, \
        "the connected revocation published a promotable pre-revocation rollback target"
    assert _live_client_ids(settings) == [kept], "a rollback resurrected the revoked uuid"
    assert c.get("/api/status").json()["active_node_id"] == nid


def test_a_reapplied_revocation_whose_sweep_fails_is_still_not_undoable(settings, stub_xray,
                                                                       monkeypatch):
    """And when the sweep does not merely do nothing but REPORTS that it could not: the
    revocation is still done and still irreversible, because its own write never created the
    pairing. What may never happen — the two together — is answering "revoked" while
    `POST /rollback` can put the device back. The failure is still written down, since on the
    paths that write nothing it remains the only guard."""
    from pi_gw_panel.net_control import events as conn_events

    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    _nid, lost, kept = _armed_with_two_clients(c, h)

    monkeypatch.setattr("pi_gw_panel.api.routes.ConfigManager.invalidate_rollback",
                        lambda self: False)
    body = c.delete(f"/api/rw/clients/{lost}", headers=h).json()
    monkeypatch.undo()

    assert body["revocation"] == "reapplied"
    assert _live_client_ids(settings) == [kept]
    assert c.get("/api/status").json()["rollback_available"] is False
    assert c.post("/api/rollback", headers=h).json()["ok"] is False, \
        "a revocation reported as done left a rollback that reinstates the revoked device"
    assert _live_client_ids(settings) == [kept]
    details = [e["detail"] for e in conn_events.recent(c.app.state.app_state.store)
               if e["kind"] == "rw-revoke"]
    assert any("could not drop the rollback target" in d for d in details), \
        "the sweep failed silently — it is still the only guard on the paths that write nothing"


def test_a_revocation_whose_rebuild_fails_falls_back_instead_of_reporting_success(settings,
                                                                                  stub_xray,
                                                                                  monkeypatch):
    """The other half of making the connected rebuild irreversible: it can now refuse BEFORE the
    live config is touched (a provenance marker that cannot be durably invalidated), so the file
    xray is running still names the revoked client. Reporting that as `reapplied` would tell the
    operator a lost device was cut off while it was still being served.

    Nor may it raise: the handler is one DB transaction, so a 502 out of the revocation rolls the
    client deletion and every event back with it — an error, a device still granted in the store,
    and an xray still serving it. It falls through to the write-only path, and when that cannot
    write either, to the stop — which is exactly what the branch below already means.
    """
    from pi_gw_panel.net_control import events as conn_events

    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    _nid, lost, kept = _armed_with_two_clients(c, h)

    monkeypatch.setattr(
        "pi_gw_panel.xray_config.validate.ConfigManager._invalidate_provenance_durably",
        lambda self: False)
    r = c.delete(f"/api/rw/clients/{lost}", headers=h)
    monkeypatch.undo()

    assert r.status_code == 200
    assert r.json()["revocation"] == "stopped", \
        "a revocation that never reached the config reported that it had been applied"
    assert c.get("/api/status").json()["running"] is False, \
        "a revocation that could not be applied left xray serving the revoked credential"
    details = [e["detail"] for e in conn_events.recent(c.app.state.app_state.store)
               if e["kind"] == "rw-revoke"]
    assert any("falling back to rewriting the live config" in d for d in details)
    assert "stopped xray to apply a remote-access revocation" in details
    # the same broken write is what the sweep needs, so it reports its failure too — and can,
    # because the revocation reached a branch that returns instead of raising the log away
    assert any("could not drop the rollback target" in d for d in details)

    # ...and the revocation itself committed, so the fail-closed state is recoverable without
    # resurrecting anything: the store no longer grants the client, whatever brings xray back.
    assert lost not in [x["id"] for x in c.get("/api/rw").json()["clients"]]
    assert c.post("/api/xray/start", headers=h).status_code == 200
    assert _live_client_ids(settings) == [kept]


def test_checking_whether_a_rollback_is_available_never_warns(settings, stub_xray, caplog):
    """`/api/status` is polled continuously and reads `rollback_available`, so warning on the
    CHECK turned a perfectly normal revocation into a warning every few seconds, for as long as
    the panel ran — burying the entries an operator needs in the expected state of a feature
    working. The warning belongs to an ATTEMPT, which is the moment somebody wanted an answer."""
    import logging

    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, _kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/apply", headers=h)            # a second apply → a provable target
    assert c.get("/api/status").json()["rollback_available"] is True
    c.delete(f"/api/rw/clients/{lost}", headers=h)          # ...deliberately invalidated

    def _warnings() -> list[str]:
        return [r.getMessage() for r in caplog.records
                if r.levelno >= logging.WARNING and r.name.endswith("xray_config.validate")]

    with caplog.at_level(logging.DEBUG, logger="pi_gw_panel.xray_config.validate"):
        for _ in range(3):
            assert c.get("/api/status").json()["rollback_available"] is False
        assert _warnings() == [], "a status poll warns about the expected post-revocation state"

        assert c.post("/api/rollback", headers=h).json()["ok"] is False
    assert any("deliberately invalidated" in m for m in _warnings()), \
        "a refused rollback attempt no longer says why"


def test_status_advertises_whether_a_rollback_would_actually_work(settings, stub_xray):
    """`prev_active_node_id` is not the answer: a revocation deliberately drops the pairing, so
    the node id can be set while `POST /rollback` answers `{"ok": false}`. API consumers had no
    way to tell the two apart."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, _kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/apply", headers=h)      # a second apply → a provable undo target
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    st = c.get("/api/status").json()
    assert st["prev_active_node_id"] == nid and st["rollback_available"] is True

    c.delete(f"/api/rw/clients/{lost}", headers=h)
    st = c.get("/api/status").json()
    assert st["prev_active_node_id"] == nid, "the node the operator would return to is unchanged"
    assert st["rollback_available"] is False
    assert c.post("/api/rollback", headers=h).json()["ok"] is st["rollback_available"]


# --- two overlapping saves must classify against what is live, not what they arrived to ---

class _LockGate:
    """Stand-in for routes' `apply_lock` that parks the FIRST request reaching it until the
    test releases it, then behaves exactly like the real lock. Reproduces the one interleaving
    that matters — a save that arrives first but is serialized second — without sleeping."""

    def __init__(self, real):
        self._real, self._armed = real, True
        self.reached, self.release = threading.Event(), threading.Event()

    def __enter__(self):
        if self._armed:
            self._armed = False
            self.reached.set()
            if not self.release.wait(20):
                raise AssertionError("the parked request was never released")
        return self._real.__enter__()

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)


def _interleaved_saves(c, h, arrives_first: dict, lands_first: dict) -> tuple[dict, dict]:
    """Run two PUT /api/rw saves with the ordering inverted: `arrives_first` enters the handler
    and is parked at `apply_lock`, `lands_first` runs to completion in the meantime, then the
    parked one takes the lock. Returns (parked response, completed response)."""
    from pi_gw_panel.api import routes as routes_mod

    gate = _LockGate(routes_mod.apply_lock)
    parked_client = TestClient(c.app)                     # a second client, one shared app
    parked_client.cookies.update(c.cookies)
    out: dict[str, dict] = {}

    def _parked() -> None:
        out["parked"] = parked_client.put("/api/rw", json=arrives_first, headers=h).json()

    t = threading.Thread(target=_parked, daemon=True)
    routes_mod.apply_lock = gate
    try:
        t.start()
        assert gate.reached.wait(20), "the first request never reached apply_lock"
        out["landed"] = c.put("/api/rw", json=lands_first, headers=h).json()
        gate.release.set()
        t.join(20)
    finally:
        routes_mod.apply_lock = gate._real
    assert not t.is_alive(), "the parked request never finished"
    return out["parked"], out["landed"]


def test_an_overlapping_save_classifies_against_what_the_other_made_live(settings, stub_xray):
    """H2: the credential snapshot was taken on arrival, outside `apply_lock`. Two overlapping
    saves serialize their writes but both compared against the surface they saw on arrival, so
    the one that landed second measured a credential surface another request had already
    replaced. Here it drops the short id the other save had just installed and reads that as a
    widening — "stored, picked up on the next connect" — leaving the revoked short id accepted
    by the running inbound. That is the original silent-no-op bug, reached through a race."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, _lost, _kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    assert _live_reality(settings)["shortIds"] == ["ab12cd34"]

    parked, landed = _interleaved_saves(c, h, RW_ARMED,
                                        {**RW_ARMED, "short_ids": "99998888"})
    assert landed["revocation"] == "rebuilt"              # rotated ab12cd34 → 99998888
    assert parked["revocation"] == "rebuilt", \
        "the save that landed second classified against a credential surface already replaced"
    assert _live_reality(settings)["shortIds"] == ["ab12cd34"], \
        "the running inbound still accepts a short id the second save removed"


def test_an_overlapping_save_is_not_a_revocation_just_because_the_other_narrowed(settings,
                                                                                 stub_xray):
    """The inverse of the same stale snapshot, and not a free error either: the fail-safe path
    can stop xray outright. Both saves ask for the same credentials; the first one narrows to
    them and the second changes nothing that is live, so only the first is a revocation."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, _lost, _kept = _armed_with_two_clients(c, h)
    assert c.put("/api/rw", json={**RW_ARMED, "short_ids": "ab12cd34,99998888"},
                 headers=h).json()["revocation"] == ""            # widening, applied normally
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    assert _live_reality(settings)["shortIds"] == ["ab12cd34", "99998888"]

    parked, landed = _interleaved_saves(c, h, RW_ARMED, RW_ARMED)
    assert landed["revocation"] == "rebuilt"              # dropped 99998888 from the live inbound
    assert parked["revocation"] == "", \
        "a save that took away nothing still live was classified as a revocation"
    assert c.get("/api/status").json()["running"] is True
    assert _live_reality(settings)["shortIds"] == ["ab12cd34"]


# --- reality settings must be format-checked before they are stored ---------------------

def test_a_malformed_dest_or_key_is_rejected_and_never_persisted(settings, stub_xray):
    """`dest`/`server_names`/`private_key` reached `realitySettings` verbatim. PUT committed
    them because the apply that would have caught them is skipped with no active node — and one
    bad value then made `xray -test` fail on EVERY later apply, so the tunnel could not come up."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    for payload in ({"dest": "this is not host:port"},
                    {"dest": "www.microsoft.com"},               # no port at all
                    {"dest": "www.microsoft.com:0"},
                    {"dest": "www.microsoft.com:notaport"},
                    {"private_key": "NOT-A-VALID-X25519-KEY!!"},
                    {"private_key": "aGVsbG8td29ybGQtdGhpcy1pcy1ub3QtYS1yZWFsLWtleQ"},  # 34 B
                    {"public_key": "nope"},
                    {"server_names": "www.microsoft.com,not a host name"},
                    {"endpoint": "home.example.org, extra-field=1"}):
        assert c.put("/api/rw", json=payload, headers=h).status_code == 422, payload
    body = c.get("/api/rw").json()
    # still the documented defaults — nothing from the loop above was committed
    assert body["dest"] == rw.DEFAULTS["rw_dest"]
    assert body["server_names"] == rw.DEFAULTS["rw_server_names"]
    assert body["endpoint"] == "" and body["public_key"] == ""
    assert body["has_private_key"] is False
    # and the good values still go through
    assert c.put("/api/rw", json={"dest": "www.microsoft.com:443", "private_key": PRIV,
                                  "public_key": PUB, "endpoint": "203.0.113.7",
                                  "server_names": "www.microsoft.com"},
                 headers=h).status_code == 200


def test_resolve_refuses_malformed_stored_reality_settings(settings, stub_xray):
    """Second lock, for values that arrive through a restore or a hand-edited DB rather than
    the API. Raising here degrades the feature to off (the caller catches it) instead of
    emitting a config every later apply would choke on."""
    for key, bad in (("rw_dest", "this is not host:port"),
                     ("rw_dest", "www.microsoft.com:99999"),
                     ("rw_server_names", "not a host name"),
                     ("rw_private_key", "NOT-A-VALID-X25519-KEY!!")):
        s = _enabled_store(**{key: bad})
        with pytest.raises(ValueError):
            rw.resolve(s)


# --- generated profiles are a delimited format, not JSON: no injection ------------------

def test_a_newline_in_the_endpoint_can_never_reach_a_generated_profile(settings, stub_xray):
    """A newline in `rw_endpoint` emitted `[Rule]\\nFINAL,DIRECT` into the `[Proxy]` section —
    an imported phone profile that looks right and sends everything straight past the VPN.
    `rw_endpoint` is restorable from a backup, so the boundary check alone is not enough."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    c.put("/api/rw", json={"private_key": PRIV, "public_key": PUB,
                           "endpoint": "home.example.org", "short_ids": "ab12cd34",
                           "server_names": "www.microsoft.com"}, headers=h)
    cid = c.post("/api/rw/clients", json={"email": "iphone"},
                 headers=h).json()["clients"][0]["id"]
    poisoned = "home.example.org\n[Rule]\nFINAL,DIRECT"
    assert c.put("/api/rw", json={"endpoint": poisoned}, headers=h).status_code == 422
    assert c.get("/api/rw").json()["endpoint"] == "home.example.org"

    # …and planted straight into the store, the way a restored backup would
    store = c.app.state.app_state.store
    for key, value in (("rw_endpoint", poisoned),
                       ("rw_public_key", f"{PUB}\nFINAL,DIRECT"),
                       ("rw_server_names", "www.microsoft.com\nFINAL,DIRECT")):
        store.set_setting("rw_endpoint", "home.example.org")
        store.set_setting("rw_public_key", PUB)
        store.set_setting("rw_server_names", "www.microsoft.com")
        store.set_setting(key, value)
        for path in (f"/api/rw/clients/{cid}/config", f"/api/rw/clients/{cid}/link"):
            r = c.get(path)
            # refused outright — no artifact is generated at all, so nothing can be imported
            assert r.status_code == 422, f"{key} -> {path}"
            assert set(r.json()) == {"detail"}, f"{key} -> {path}"
        with pytest.raises(ValueError):
            rw.shadowrocket_conf(store, rw.get_clients(store)[0], ["192.168.1.0/24"])


def test_a_malformed_client_id_never_reaches_a_generated_profile(settings, stub_xray):
    """`rw_clients` is restorable too, and the uuid is interpolated into both artifacts."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    c.put("/api/rw", json={"private_key": PRIV, "public_key": PUB,
                           "endpoint": "home.example.org", "short_ids": "ab12cd34",
                           "server_names": "www.microsoft.com"}, headers=h)
    store = c.app.state.app_state.store
    store.set_setting("rw_clients", json.dumps(
        [{"id": "abc\nFINAL,DIRECT", "email": "iphone", "enabled": True}]))
    assert c.get("/api/rw").json()["clients"] == []       # dropped on read, not repaired
    assert rw.resolve(store) is None                      # and never emitted into the config


# --- the write ceiling must fit under the backup ceiling --------------------------------

def test_the_widest_host_map_we_accept_is_still_restorable(settings, stub_xray):
    """Bounding only the entry COUNT let 32 long names write a 2646-char `rw_hosts`, over the
    2048-char per-setting cap a backup enforces — so the panel's own GET /api/backup came back
    unrestorable. The write ceiling has to sit under the backup ceiling, not beside it."""
    stem = "a" * (rw.MAX_HOST_NAME - 7)
    hosts = {f"{stem}{i:02d}.v2pi": "255.255.255.255" for i in range(rw.MAX_HOSTS)}
    assert all(len(name) == rw.MAX_HOST_NAME for name in hosts)
    assert len(json.dumps(rw.validate_hosts(hosts))) <= 2048

    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    assert c.put("/api/rw", json={"hosts": hosts}, headers=h).status_code == 200
    doc = c.get("/api/backup").json()
    assert len(doc["settings"]["rw_hosts"]) <= 2048
    # the real validator, via the route a restore actually takes
    assert c.post("/api/restore", json=doc, headers=h).status_code == 200
    assert len(c.get("/api/rw").json()["hosts"]) == rw.MAX_HOSTS


def test_an_over_long_host_name_is_rejected(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    too_long = "b" * (rw.MAX_HOST_NAME - 4) + ".v2pi"      # one char over
    assert len(too_long) == rw.MAX_HOST_NAME + 1
    r = c.put("/api/rw", json={"hosts": {too_long: "192.168.1.88"}}, headers=h)
    assert r.status_code == 422 and "longer than" in r.json()["detail"]
