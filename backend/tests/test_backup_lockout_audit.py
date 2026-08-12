"""Restore is the second door onto the one config the operator cannot undo through the UI.

`PUT /api/network` refuses a segment that would be installed on the management leg — the
kill-switch drop, the tproxy redirect and the segment's DHCP server aimed at the interface the
panel is reached on. A restore writes the very same settings, and it used to write them
unchecked: every validator on that path judged one field on its own and nothing compared the
document against this gateway's management leg.

A restore is the worse door of the two. The operator is recovering from something, usually
remotely, and a restore that quietly reconfigures the management leg leaves them with no panel
and no visible cause. So it is refused, with the SAME function the route uses, and refused where
a refusal still costs nothing: before the pre-restore snapshot, before xray is stopped, before a
row is written.

The other half of the requirement gets tests too: a restore that refuses a legitimate document,
or refuses everything on a gateway whose management leg cannot be read, is its own lockout.

Defaults these tests lean on: mgmt eth0 / 192.168.1.120, segment eth0.2 / 192.168.10.2.
"""
import os

from conftest import _build_dryrun_state, _login
from fastapi.testclient import TestClient

from pi_gw_panel.app import create_app
from pi_gw_panel.backup import backups_dir, validate_document, write_pre_restore_snapshot
from pi_gw_panel.config import Settings


def _client(settings, stub_xray):
    state = _build_dryrun_state(settings, stub_xray)
    client = TestClient(create_app(settings, state=state))
    return client, {"X-CSRF-Token": _login(client)}, state


def _uploaded(client, **net):
    """A backup file as it reaches the panel from the operator.

    Deliberately the JSON round-trip a download-then-upload is: what comes back from
    `GET /api/backup` over HTTP is a plain document with no memory of having been exported here,
    which is exactly how the panel must treat anything handed to `POST /api/restore`.
    """
    document = client.get("/api/backup").json()
    document["settings"].update(net)
    return document


def _node(client, headers, name="keep"):
    return client.post("/api/nodes",
                       json={"name": name, "address": "7.7.7.7", "port": 443, "uuid": "u"},
                       headers=headers).json()["id"]


def test_a_document_that_moves_the_segment_onto_the_management_leg_is_refused(settings, stub_xray):
    """`segment_iface == mgmt_iface` is refused from a document exactly as from the route — and
    refused early enough that the live gateway is untouched: xray still running, nodes still
    there, settings unwritten, not even a pre-restore snapshot on disk."""
    client, headers, state = _client(settings, stub_xray)
    node_id = _node(client, headers)
    assert client.post(f"/api/nodes/{node_id}/apply", headers=headers).status_code == 200
    applied_before = len(state.net.applied)
    document = _uploaded(client, segment_iface=settings.mgmt_iface)

    response = client.post("/api/restore", json=document, headers=headers)

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "segment_iface" in detail and "mgmt_iface" in detail, \
        f"the operator was not told which two fields collide: {detail!r}"
    assert state.store.get_setting("segment_iface") in (None, ""), \
        "the collision was refused only after it had been persisted"
    # A restore DELETEs every node, stops xray and re-asserts the guard before it writes. None of
    # that may have happened: a refused restore is not a half-done one.
    assert [n["id"] for n in client.get("/api/nodes").json()] == [node_id]
    assert client.get("/api/status").json()["running"] is True, \
        "the refusal came after the tunnel had already been stopped"
    assert len(state.net.applied) == applied_before, "a refused restore still reached the host"
    assert os.listdir(backups_dir(state.settings)) == [], \
        "the refusal came after the pre-restore snapshot, not before it"


def test_a_document_whose_segment_shares_the_management_network_is_refused(settings, stub_xray):
    """The segment /24 overlapping the management /24 puts the DHCP pool and the LAN-access NAT
    in the network the panel is reached on."""
    client, headers, state = _client(settings, stub_xray)
    node_id = _node(client, headers)
    document = _uploaded(client, segment_ip="192.168.1.5", dhcp_start="192.168.1.30",
                         dhcp_end="192.168.1.200")

    response = client.post("/api/restore", json=document, headers=headers)

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "segment_ip" in detail and settings.mgmt_ip in detail, \
        f"the /24 collision did not name the management address: {detail!r}"
    assert state.store.get_setting("segment_ip") in (None, "")
    assert [n["id"] for n in client.get("/api/nodes").json()] == [node_id]
    assert os.listdir(backups_dir(state.settings)) == []


def test_a_document_whose_client_dns_lives_in_the_segment_is_refused(settings, stub_xray):
    """The third refusal of the same set: a resolver inside the segment is never tunneled (a
    private destination RETURNs above the tproxy rule) and nothing answers it."""
    client, headers, state = _client(settings, stub_xray)
    document = _uploaded(client, client_dns="192.168.10.50")

    response = client.post("/api/restore", json=document, headers=headers)

    assert response.status_code == 400
    assert "client_dns" in response.json()["detail"]
    assert state.store.get_setting("client_dns") in (None, "")


def test_a_legitimate_document_still_restores_end_to_end(settings, stub_xray):
    """The guard must refuse the collisions, not the restores: a document with a segment of its
    own comes back whole, nodes and settings alike."""
    client, headers, state = _client(settings, stub_xray)
    node_id = _node(client, headers)
    document = _uploaded(client, segment_iface="eth1.20", segment_ip="10.20.0.1",
                         dhcp_start="10.20.0.30", dhcp_end="10.20.0.200", client_dns="1.1.1.1")
    client.delete(f"/api/nodes/{node_id}", headers=headers)
    assert client.get("/api/nodes").json() == []

    response = client.post("/api/restore", json=document, headers=headers)

    assert response.status_code == 200, response.text
    assert [n["id"] for n in client.get("/api/nodes").json()] == [node_id]
    assert state.store.get_setting("segment_iface") == "eth1.20"
    assert state.store.get_setting("segment_ip") == "10.20.0.1"
    assert os.listdir(backups_dir(state.settings)) != [], "the pre-restore snapshot was skipped"


def test_the_document_is_checked_against_this_gateways_own_management_leg(settings, stub_xray,
                                                                         monkeypatch):
    """Against THIS gateway's leg, not a hardcoded one: the same document is refused or accepted
    according to what the panel's own configuration says the management interface is."""
    monkeypatch.setenv("PI_GW_MGMT_IFACE", "enp1s0")
    client, headers, state = _client(settings, stub_xray)

    refused = client.post("/api/restore", json=_uploaded(client, segment_iface="enp1s0"),
                          headers=headers)
    assert refused.status_code == 400 and "enp1s0" in refused.json()["detail"]

    # ...and the interface that WOULD have collided on the default configuration is fine here.
    assert client.post("/api/restore", json=_uploaded(client, segment_iface="eth0"),
                       headers=headers).status_code == 200
    assert state.store.get_setting("segment_iface") == "eth0"


def test_an_unreadable_management_leg_skips_the_check_instead_of_refusing(settings, stub_xray,
                                                                         monkeypatch):
    """Same rule as the route's: a panel that cannot see its own management path must not answer
    every document with a refusal — that is the same lockout by another route."""
    monkeypatch.setenv("PI_GW_MGMT_IFACE", "not a valid interface")
    client, headers, state = _client(settings, stub_xray)
    document = _uploaded(client, segment_iface="eth0")

    assert client.post("/api/restore", json=document, headers=headers).status_code == 200
    assert state.store.get_setting("segment_iface") == "eth0"
    # ...and the explicit form of "no management leg", as the route's own guard defines it.
    validate_document(dict(document), Settings(mgmt_iface="", mgmt_ip=""))      # must not raise


def test_a_gateway_already_in_a_colliding_state_can_still_back_up_and_restore(settings, stub_xray):
    """The document a box exports RECORDS the state it is already in; it proposes nothing. If
    that were refused too, a gateway that reached a colliding state before the guard existed
    could neither export its configuration nor take the pre-restore snapshot every restore
    begins with — losing the backup and the restore that fix it."""
    client, headers, state = _client(settings, stub_xray)
    state.store.set_setting("segment_iface", settings.mgmt_iface)

    assert client.get("/api/backup").status_code == 200
    write_pre_restore_snapshot(state)               # the safety net, on the colliding state

    document = _uploaded(client, segment_iface="eth0.2")
    assert client.post("/api/restore", json=document, headers=headers).status_code == 200
    assert state.store.get_setting("segment_iface") == "eth0.2"
