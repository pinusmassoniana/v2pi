"""The config `PUT /api/network` must refuse rather than apply.

Every net validator checked ONE field on its own and nothing ever compared the segment against the
management leg, so the panel ACCEPTED `segment_iface == mgmt_iface` — a kill-switch drop and a
tproxy redirect installed on the interface the panel itself is reached on, plus the segment's DHCP
server served onto the home LAN. That is the one class of bad config the operator cannot undo
through the UI, because applying it removes the UI. It is refused, not warned about, and refused
before anything is persisted or reaches the host.

The other half of the same requirement gets its own tests: a guard that refuses a legitimate
config, or every config on a host whose management leg is unknown, is its own lockout.

Defaults these tests lean on: mgmt eth0 / 192.168.1.120, segment eth0.2 / 192.168.10.2,
client_dns 1.1.1.1.
"""
from conftest import _build_dryrun_state, _login
from fastapi.testclient import TestClient

from pi_gw_panel.app import create_app
from pi_gw_panel.config import Settings, check_change_safe


def _net_client(settings, stub_xray):
    state = _build_dryrun_state(settings, stub_xray)
    c = TestClient(create_app(settings, state=state))
    return c, {"X-CSRF-Token": _login(c)}, state


def test_the_management_interface_is_refused_as_the_segment(settings, stub_xray):
    """`segment_iface == mgmt_iface` renders the kill-switch drop and the tproxy redirect onto the
    leg the operator reaches the panel on. Refused, and nothing is stored or applied."""
    c, h, state = _net_client(settings, stub_xray)
    applied_before = len(state.net.applied)

    r = c.put("/api/network", json={"segment_iface": settings.mgmt_iface}, headers=h)

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "segment_iface" in detail and "mgmt_iface" in detail, \
        f"the operator was not told which two fields collide: {detail!r}"
    assert state.store.get_setting("segment_iface") in (None, ""), \
        "the collision was refused only after it had been persisted"
    assert len(state.net.applied) == applied_before, "a refused config still reached the host"


def test_a_segment_in_the_management_network_is_refused(settings, stub_xray):
    """The segment /24 overlapping the management /24 puts the DHCP pool and the LAN-access NAT in
    the network the panel is reached on."""
    c, h, state = _net_client(settings, stub_xray)

    r = c.put("/api/network", json={"segment_ip": "192.168.1.5",
                                    "dhcp_start": "192.168.1.30",
                                    "dhcp_end": "192.168.1.200"}, headers=h)

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "segment_ip" in detail and settings.mgmt_ip in detail, \
        f"the /24 collision did not name the management address: {detail!r}"
    assert state.store.get_setting("segment_ip") in (None, "")


def test_a_client_dns_inside_the_segment_with_no_resolver_is_refused(settings, stub_xray):
    """A private destination RETURNs above the tproxy rule, so a resolver inside the segment is
    never tunneled and nothing answers it. The gateway's own address is the exception."""
    c, h, state = _net_client(settings, stub_xray)

    r = c.put("/api/network", json={"client_dns": "192.168.10.50"}, headers=h)
    assert r.status_code == 422
    assert "client_dns" in r.json()["detail"] and "segment" in r.json()["detail"]
    assert state.store.get_setting("client_dns") in (None, "")

    # ...and the gateway itself is a resolver (the panel's own dnsmasq answers there).
    assert c.put("/api/network", json={"client_dns": settings.segment_ip},
                 headers=h).status_code == 200


def test_a_collision_with_a_field_the_request_does_not_mention_is_refused(settings, stub_xray):
    """The check is on the EFFECTIVE config: a one-field edit collides just as well with a stored
    value it never names. Here only the interface is sent, and it is the stored segment_ip's /24
    that is already the management one."""
    c, h, state = _net_client(settings, stub_xray)
    state.store.set_setting("segment_ip", "192.168.1.9")

    r = c.put("/api/network", json={"dhcp_lease": "6h"}, headers=h)

    assert r.status_code == 422, "the stored collision was invisible to a request that omitted it"
    assert state.store.get_setting("dhcp_lease") in (None, ""), \
        "an unrelated field was persisted by a request that had to be refused"


def test_a_legitimate_config_still_applies(settings, stub_xray):
    """The other half of the lockout: a guard that refuses everything is its own outage. A VLAN of
    the management NIC, its own /24, a public resolver — the normal re-addressing — still lands."""
    c, h, _ = _net_client(settings, stub_xray)

    r = c.put("/api/network", json={"segment_iface": "eth1.7", "segment_ip": "10.44.7.2",
                                    "dhcp_start": "10.44.7.30", "dhcp_end": "10.44.7.200",
                                    "client_dns": "1.1.1.1"}, headers=h)

    assert r.status_code == 200, r.text
    assert r.json()["segment"]["iface"] == "eth1.7"
    assert c.put("/api/network", json={"dhcp_end": "10.44.7.250"}, headers=h).status_code == 200
    assert c.put("/api/network", json={"segment_iface": f"{settings.mgmt_iface}.2"},
                 headers=h).status_code == 200, "a VLAN of the management NIC is the normal setup"


def test_an_unset_management_leg_does_not_refuse_everything(settings, stub_xray):
    """A panel that does not know its own management path cannot tell a collision from a working
    config. It skips the checks that need it — refusing every config would be the same lockout by
    another route."""
    settings.mgmt_iface = ""
    settings.mgmt_ip = ""
    c, h, _ = _net_client(settings, stub_xray)

    assert c.put("/api/network", json={"dhcp_end": "192.168.10.250"}, headers=h).status_code == 200
    assert c.put("/api/network", json={"segment_iface": "eth0"}, headers=h).status_code == 200
    assert check_change_safe({"segment_iface": "eth0", "segment_ip": "192.168.1.5",
                              "client_dns": "1.1.1.1"}, settings) == []


def test_check_change_safe_compares_the_segment_against_the_management_leg(settings):
    """The unit-level pair, so a check that compares the wrong two fields cannot pass by accident:
    the same values are safe against one management leg and refused against another."""
    against_eth1 = Settings(mgmt_iface="eth1", mgmt_ip="10.0.0.1")
    proposed = {"segment_iface": "eth0", "segment_ip": "192.168.1.5", "client_dns": "1.1.1.1"}

    assert check_change_safe(proposed, against_eth1) == []
    problems = check_change_safe(proposed, Settings(mgmt_iface="eth0", mgmt_ip="192.168.1.120"))
    assert len(problems) == 2, problems
    assert any(p.startswith("segment_iface:") for p in problems)
    assert any(p.startswith("segment_ip:") for p in problems)
