"""A4 over the API: pinning a device changes the segment's DHCP and the ruleset, or changes
nothing at all. The refusals are the point — every accepted row is rendered into a config the
panel hands to a root-supervised dnsmasq."""
from conftest import _client, _login

TV = {"mac": "AA:BB:CC:DD:EE:FF", "ip": "192.168.10.42", "name": "tv"}


def _pin(c, headers, **over):
    return c.post("/api/net/reservations", json={**TV, **over}, headers=headers)


def test_pinning_a_device_renders_dhcp_and_counters_and_normalises_the_mac(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}

    response = _pin(c, h)
    assert response.status_code == 201, response.json()
    body = response.json()

    assert body["max_reservations"] == 64 and body["window_sec"] == 86_400
    row = body["reservations"][0]
    assert row["mac"] == "aa:bb:cc:dd:ee:ff"            # stored lowercase, as dnsmasq writes it
    assert (row["ip"], row["name"], row["online"]) == ("192.168.10.42", "tv", False)
    assert (row["up_bytes"], row["down_bytes"]) == (0, 0)
    # The ruleset the gateway now carries counts that device in both directions.
    applied = c.app.state.app_state.net.applied[-1]
    assert "counter dev_up_192_168_10_42" in applied and "counter dev_down_192_168_10_42" in applied
    assert "ip saddr 192.168.10.42 counter name dev_up_192_168_10_42" in applied
    assert "chain acct_up" in applied and "chain acct_down" in applied


def test_the_values_that_are_refused(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}

    assert _pin(c, h, mac="nonsense").status_code == 422          # dnsmasq --test would accept it
    assert _pin(c, h, ip="192.168.99.9").status_code == 422       # outside the segment
    assert _pin(c, h, ip="192.168.10.2").status_code == 422       # the gateway's own address
    assert _pin(c, h, name="living room").status_code == 422      # not a hostname
    assert _pin(c, h, name="tv\ndhcp-script=/tmp/x.sh").status_code == 422
    assert c.get("/api/net/reservations").json()["reservations"] == []


def test_a_mac_or_an_address_can_only_be_pinned_once(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    assert _pin(c, h).status_code == 201

    assert _pin(c, h, ip="192.168.10.43").status_code == 409                      # same MAC
    assert _pin(c, h, mac="aa:bb:cc:dd:ee:01").status_code == 409                 # same address
    assert len(c.get("/api/net/reservations").json()["reservations"]) == 1


def test_rename_and_unpin(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    res_id = _pin(c, h).json()["reservations"][0]["id"]

    renamed = c.patch(f"/api/net/reservations/{res_id}", json={"name": "living-room"}, headers=h)
    assert renamed.json()["reservations"][0]["name"] == "living-room"
    assert c.patch(f"/api/net/reservations/{res_id}", json={"name": "bad name"}, headers=h).status_code == 422

    body = c.delete(f"/api/net/reservations/{res_id}", headers=h).json()
    assert body["reservations"] == []
    assert c.delete(f"/api/net/reservations/{res_id}", headers=h).status_code == 404
    # Unpinned: the counters go with it, and the ruleset is the plain one again.
    assert "dev_up_192_168_10_42" not in c.app.state.app_state.net.applied[-1]


def test_every_write_needs_the_csrf_header(settings, stub_xray):
    c = _client(settings, stub_xray)
    _login(c)
    assert c.post("/api/net/reservations", json=TV).status_code == 403
    assert c.patch("/api/net/reservations/1", json={"name": "x"}).status_code == 403
    assert c.delete("/api/net/reservations/1").status_code == 403


def test_a_device_rule_can_be_saved_and_routes_by_source(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    _pin(c, h)

    saved = c.put("/api/routing", json={
        "rules": [{"type": "device", "value": "192.168.10.42", "action": "direct", "label": "tv"}],
        "default_action": "proxy",
    }, headers=h)

    assert saved.status_code == 200
    assert saved.json()["rules"][0] == {
        "id": saved.json()["rules"][0]["id"], "position": 0, "type": "device",
        "value": "192.168.10.42", "action": "direct", "enabled": True, "label": "tv", "dataset": "",
    }
    # And a v6 address is refused with the reason, not stored and then never matched.
    bad = c.put("/api/routing", json={
        "rules": [{"type": "device", "value": "fd00::5", "action": "direct"}],
        "default_action": "proxy",
    }, headers=h)
    assert bad.status_code == 422 and "IPv4" in bad.json()["detail"]
