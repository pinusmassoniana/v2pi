"""The candidate ledger a rolled-back host-provisioning pass leaves behind, and who reads it.

The record of what a pass put on the host was written by `PUT /api/network` and read by nothing
but its own rollback, so the crash its docstring cites — panel dies between provisioning the
candidate interface and undoing it — stranded host state no later pass would ever look at. And
`restore_backup`, which can retarget `segment_iface` through the same `host_provision` inside the
same transaction, had no candidate ledger at all.

One implementation now (`net_control/provision.py`), three callers: the route, the restore, and
the boot path.
"""
import json
import logging
import re
import subprocess

import pytest
from conftest import _login
from fastapi.testclient import TestClient

from pi_gw_panel import backup as backup_mod
from pi_gw_panel.app import create_app
from pi_gw_panel.controller import restore_backup
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.net_control import linux, provision
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.net_control.plan import NetResult
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.state import build_state

UNDO_KEY = "pending_provision_undo"


class _HostNet(DryRunBackend):
    """A dry-run backend plus the `_run` seam that makes host provisioning take its linux path.

    Models the two things the undo reasons about — which links are on the host, and which
    addresses are on them — and speaks like iproute2 while doing it: absence is an explicit
    not-found on stderr, `ip -o addr show` lists what the interface actually carries, and anything
    else (`silent`) is a command that failed without saying anything about the device.

    The addresses matter as much as the links now: an interface can be the operator's own, with
    the operator's own address on it, and the whole question the undo has to answer is which of
    the two put that address there.
    """

    def __init__(self, links=("eth0", "eth0.2"), addrs=None, refuse=(), silent=(),
                 refused_as=(1, "RTNETLINK answers: refused")):
        super().__init__()
        self.cmds: list[list[str]] = []
        self.links = set(links)
        self.addrs = {iface: set(cidrs) for iface, cidrs in (addrs or {}).items()}
        self.refuse = [tuple(cmd) for cmd in refuse]   # command prefixes that fail
        self.silent = set(silent)                      # devices the host cannot answer for
        # How a refusal presents. The runner's time limit is also a `CalledProcessError`, with
        # `TIMEOUT_RETURNCODE` and a synthetic stderr naming the command it killed — a different
        # failure to report and the same fact about the host: nothing was removed.
        self.refused_as = refused_as

    @staticmethod
    def _parse(cmd: list[str]) -> tuple[list[str], bool]:
        """`(["addr", "show", ...], ipv6)` — the command with its family/format flags stripped."""
        return [tok for tok in cmd[1:] if tok not in ("-4", "-6", "-o")], "-6" in cmd

    def _show_addrs(self, iface: str, ipv6: bool) -> str:
        """What `ip -o addr show dev <iface>` prints, one line per address."""
        return "".join(
            f"2: {iface}    {'inet6' if ':' in a else 'inet'} {a} scope global {iface}\\"
            f"       valid_lft forever preferred_lft forever\n"
            for a in sorted(self.addrs.get(iface, ())) if (":" in a) == ipv6)

    def _run(self, cmd, **kw):
        self.cmds.append(list(cmd))
        for prefix in self.refuse:
            if tuple(cmd[:len(prefix)]) == prefix:
                code, stderr = self.refused_as
                raise subprocess.CalledProcessError(code, cmd, stderr=stderr)
        rest, ipv6 = self._parse(cmd)
        head, dev = rest[:2], rest[-1]
        if head in (["link", "show"], ["addr", "show"]):
            if dev in self.silent:
                raise subprocess.CalledProcessError(1, cmd)          # no answer about the device
            if dev not in self.links:
                raise subprocess.CalledProcessError(
                    1, cmd, stderr=f'Device "{dev}" does not exist.')
            if head == ["addr", "show"]:
                return subprocess.CompletedProcess(cmd, 0, self._show_addrs(dev, ipv6), "")
        if head == ["link", "add"]:
            self.links.add(cmd[cmd.index("name") + 1])
        if head == ["link", "delete"]:
            self.links.discard(rest[-1])
            self.addrs.pop(rest[-1], None)
        if head in (["addr", "replace"], ["addr", "add"], ["addr", "del"]):
            if dev not in self.links:
                raise subprocess.CalledProcessError(
                    1, cmd, stderr=f'Cannot find device "{dev}"')
            on = self.addrs.setdefault(dev, set())
            on.discard(rest[2]) if head[1] == "del" else on.add(rest[2])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def deleted_links(self) -> list[str]:
        return [cmd[-1] for cmd in self.cmds if self._parse(cmd)[0][:2] == ["link", "delete"]]

    def deleted_addrs(self) -> list[list[str]]:
        return [cmd for cmd in self.cmds if self._parse(cmd)[0][:2] == ["addr", "del"]]


def _state(settings, stub_xray, net):
    settings.xray_bin = stub_xray
    state = build_state(settings, net=net)
    state.dnsmasq = state.pd_client = None   # keep these about the link/address ownership
    return state


def _pending(state, **candidate):
    state.store.set_setting(UNDO_KEY, json.dumps(
        {"iface": "eth0.9", "addr4": "192.168.10.2/24", "addr6": "", "vlan": True,
         "link_state": provision.LINK_ABSENT} | candidate))


def _bare_store():
    """A store and nothing else, for the readers that are a question about one record."""
    conn = connect(":memory:")
    init_schema(conn)
    return NodeStore(conn)


def _boot(settings, state, *keys) -> dict:
    """Run the real startup sequence (lifespan) against `state`, and read back the settings named
    by `keys` while the store is still open — shutdown closes it."""
    read = (UNDO_KEY, "managed_segment_link", "managed_segment_iface", *keys)
    with TestClient(create_app(settings, state=state)) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        return {key: (state.store.get_setting(key) or "") for key in read}


# --- the crash the record exists for -------------------------------------------------------

def test_a_crash_between_provisioning_and_undo_is_finished_at_the_next_boot(settings, stub_xray):
    """The record was written and cleared and never read: the only reader in the tree was the
    rollback that had already died with the process. A candidate interface left on the host was
    then invisible to the panel — the ownership metadata rolled back and names the old one — and
    outside the nft guard, which is scoped to that same old one."""
    net = _HostNet(links=("eth0", "eth0.2", "eth0.9"))    # the orphan survived the crash
    state = _state(settings, stub_xray, net)
    _pending(state)

    stored = _boot(settings, state)

    assert "eth0.9" in net.deleted_links(), "the orphaned candidate link was never reclaimed"
    assert "eth0.9" not in net.links
    assert stored[UNDO_KEY] == ""                        # nothing left pending
    assert "eth0.2" in net.links                          # ...and the real segment still came up


def test_the_boot_undo_runs_before_the_pass_that_would_otherwise_half_undo_it(settings, stub_xray):
    """ORDERING CONTRACT. The undo goes before `host_provision`, never after.

    Here the pass creates the segment VLAN, records it in its own ownership ledger, installs its
    addresses, and then dies on the last step of all — bringing the link up, which is where the
    bring-up now is. An undo running AFTER that is reading a record written BEFORE the pass, whose
    `link_state` says the interface was not on the host, against a host the pass has since changed
    — where that same interface is present precisely because the pass created and claimed it. How
    much of the ownership metadata has caught up depends on exactly where the pass died, so it is
    not something to lean on; the order is. Running the undo BEFORE cannot make the mistake at
    all, and the pass that follows re-asserts the configured segment, so an undo that reaches too
    far is repaired inside the same boot.
    """
    net = _HostNet(links=("eth0",), refuse=[("ip", "link", "set", "eth0.2", "up")])
    state = _state(settings, stub_xray, net)
    assert state.store.get_setting("managed_segment_iface") in (None, "")   # never provisioned
    _pending(state, iface="eth0.2")

    stored = _boot(settings, state)

    assert "eth0.2" not in net.deleted_links(), \
        "the undo removed the link the provisioning pass had just created"
    assert "eth0.2" in net.links
    # ...and the panel's own ledger still agrees with the host it is describing.
    assert "eth0.2" in stored["managed_segment_link"]


# --- what an undo may not do ---------------------------------------------------------------

@pytest.mark.parametrize("raw", ["{not json", "[]", '{"vlan": true}', "   "])
def test_an_unusable_record_at_boot_starts_the_panel_and_removes_nothing(settings, stub_xray, raw):
    """A stale or malformed record must not stop the panel from starting, and must not delete
    host state the panel does not own: it names no interface, so there is nothing it could
    safely remove and nothing a later boot could do better with it."""
    net = _HostNet(links=("eth0", "eth0.2", "eth0.9"))
    state = _state(settings, stub_xray, net)
    state.store.set_setting(UNDO_KEY, raw)

    stored = _boot(settings, state)   # asserts /api/health answers: the panel actually came up

    assert net.deleted_links() == [] and net.deleted_addrs() == []
    assert "eth0.9" in net.links
    assert stored[UNDO_KEY].strip() == ""


def test_an_undo_that_cannot_prove_the_link_is_there_leaves_it_and_says_so(settings, stub_xray,
                                                                          caplog):
    """The three-valued probe, used as three values. `ip link show` failing without an explicit
    not-found is `LINK_UNKNOWN` — a refusal, a netlink error, or the runner's own time limit,
    none of which says anything about the device — so the undo deletes nothing and reports.
    The record is KEPT: unlike an unprovable prior state, this one a later pass may answer."""
    net = _HostNet(links=("eth0", "eth0.2", "eth0.9"), silent=("eth0.9",))
    state = _state(settings, stub_xray, net)
    _pending(state)

    with caplog.at_level(logging.WARNING, logger="pi_gw_panel"):
        stored = _boot(settings, state)

    assert net.deleted_links() == []
    assert "eth0.9" in net.links
    assert "eth0.9" in caplog.text                       # reported, not silent
    assert stored[UNDO_KEY], "an unsettled undo must survive for the next pass"


def test_a_link_the_panel_cannot_prove_it_created_is_never_deleted(settings, stub_xray, caplog):
    """Ownership is proven by the probe taken BEFORE the pass, and only by `LINK_ABSENT`. A
    record whose prior state was never established describes an interface that may well be the
    host's own, so it is reported and left alone — and cleared, because no retry can turn a
    recorded non-answer into proof."""
    net = _HostNet(links=("eth0", "eth0.2", "eth0.9"))
    state = _state(settings, stub_xray, net)
    _pending(state, link_state=provision.LINK_UNKNOWN)

    with caplog.at_level(logging.WARNING, logger="pi_gw_panel"):
        stored = _boot(settings, state)

    assert net.deleted_links() == []
    assert "eth0.9" in net.links
    assert "eth0.9" in caplog.text
    assert stored[UNDO_KEY] == ""


def test_a_record_from_the_previous_format_never_licenses_a_delete(settings, stub_xray):
    """The superseded record stored a bool that had already folded "could not tell" into False.
    Reading that False back as proven absence would delete a link on the strength of a probe that
    never answered, so it reads as unknown instead."""
    net = _HostNet(links=("eth0", "eth0.2", "eth0.9"))
    state = _state(settings, stub_xray, net)
    state.store.set_setting(UNDO_KEY, json.dumps(
        {"iface": "eth0.9", "addr4": "192.168.10.2/24", "addr6": "", "vlan": True,
         "link_existed": False}))

    _boot(settings, state)

    assert net.deleted_links() == []
    assert "eth0.9" in net.links


def test_a_deleted_candidate_link_takes_its_own_addresses_with_it(settings, stub_xray):
    """The one deletion the undo still performs, and why it needs no separate address handling.

    Ownership of a VLAN is recorded before the panel creates it and is provable afterwards, so a
    link proven not to have been there beforehand is the panel's to remove — and removing it takes
    every address on it off the host in one kernel operation. No `ip addr del` is issued, and none
    is needed.
    """
    net = _HostNet(links=("eth0", "eth0.2", "eth0.9"), addrs={"eth0.9": ["192.168.10.2/24"]})
    state = _state(settings, stub_xray, net)
    _pending(state)                       # prior ABSENT: the pass created eth0.9

    stored = _boot(settings, state)

    assert "eth0.9" in net.deleted_links()
    assert "eth0.9" not in net.links and "eth0.9" not in net.addrs   # the address went with it
    assert net.deleted_addrs() == []
    assert stored[UNDO_KEY] == ""


# --- ...and what may AUTHORISE that one deletion -----------------------------------------------
#
# The delete is the one irreversible thing the undo still does, and its licence is a value read out of
# JSON. It was read by truthiness — `candidate.get("vlan")` — so `"false"`, a non-empty string, was a
# yes. The value sits next to an `iface` and a `link_state` in the same record, so a record saying
# `{"iface": "eth0", "vlan": "false", "link_state": "absent"}` passed every other check and removed
# eth0: an operator-owned uplink, on the strength of a claim nothing in the panel ever wrote.
#
# So the claim is identity with `True`, and it is checked against the NAME it is a claim about: the
# panel creates a VLAN and only a VLAN, so `vlan: true` over a dotless name is a record whose halves
# disagree, and a record that disagrees with itself authorises nothing. Anything refused is left on
# the host, reported, and RETAINED — the interface goes on being covered either way.

NOT_A_VLAN_CLAIM = ["false", "true", 0, 1, "1", None]


@pytest.mark.parametrize("claim", NOT_A_VLAN_CLAIM)
def test_only_a_recorded_yes_authorises_removing_a_candidate_link(settings, stub_xray, claim):
    """PRODUCT-CRITICAL. Everything else in the record says delete — a VLAN-shaped name, a prior probe
    that proved the link absent, a link that is on the host now — so the `vlan` value is the whole
    authorisation. `1` is in the list because `1 == True` in Python: equality is not enough."""
    net = _HostNet(links=("eth0", "eth0.2", "eth0.9"), addrs={"eth0.9": ["192.168.10.2/24"]})
    state = _state(settings, stub_xray, net)
    _pending(state, vlan=claim)

    stored = _boot(settings, state, provision.SURVIVOR_KEY)

    assert net.deleted_links() == [], f"vlan={claim!r} authorised an ip link delete"
    assert "eth0.9" in net.links and net.addrs["eth0.9"] == {"192.168.10.2/24"}
    assert net.deleted_addrs() == []
    # ...and the interface that was left up is still named by something the cover reads.
    assert "eth0.9" in stored[provision.SURVIVOR_KEY] or "eth0.9" in stored[UNDO_KEY], \
        "a candidate link the undo declined to remove stopped being covered"


@pytest.mark.parametrize("claim", ["false", "true", 0, 1, "1"])
def test_a_vlan_claim_that_cannot_be_read_is_reported_and_kept(settings, stub_xray, claim, caplog):
    """A value that is neither a yes nor a no is not silently a no: the record cannot be read in full,
    so it stays pending for an operator to repair and the reason reaches the log. (`null` and a missing
    key ARE a no — a dotless segment interface, or the unarmed record a restore writes.)"""
    net = _HostNet(links=("eth0", "eth0.2", "eth0.9"))
    state = _state(settings, stub_xray, net)
    _pending(state, vlan=claim)

    with caplog.at_level(logging.WARNING, logger="pi_gw_panel"):
        stored = _boot(settings, state)

    assert stored[UNDO_KEY], "a record that could not be read was cleared away"
    assert "not an answer" in caplog.text and "eth0.9" in caplog.text
    assert net.deleted_links() == []


def test_a_malformed_claim_never_deletes_the_link_the_gateway_is_reached_on(settings, stub_xray):
    """The finding at its worst, and the reason the value may not be read loosely: the record names
    the WAN uplink, the prior probe says the panel created it, and the link is on the host. Only the
    `vlan` value stands between that record and `ip link delete eth0`."""
    net = _HostNet(links=("eth0", "eth0.2"))
    state = _state(settings, stub_xray, net)
    _pending(state, iface="eth0", vlan="false", addr4="", addr6="")

    _boot(settings, state)

    assert net.deleted_links() == [], "the panel removed an interface it does not own"
    assert "eth0" in net.links


def test_a_yes_about_a_name_that_is_not_a_vlan_deletes_nothing(settings, stub_xray, caplog):
    """The other half of the same record. A real `true` is still not enough on a dotless name: the
    panel creates VLANs and only VLANs, so such a record cannot have come from a pass here — and the
    name it points at is exactly the kind the operator owns."""
    net = _HostNet(links=("eth0", "eth0.2"))
    state = _state(settings, stub_xray, net)
    _pending(state, iface="eth0", vlan=True, addr4="", addr6="")

    with caplog.at_level(logging.WARNING, logger="pi_gw_panel"):
        _boot(settings, state)

    assert net.deleted_links() == [] and "eth0" in net.links
    assert "not a VLAN interface name" in caplog.text


@pytest.mark.parametrize("planted", ["eth0.2 eth0.9", 'eth0"; drop', "eth0.2\teth0.9"])
def test_a_yes_about_a_name_that_is_not_an_interface_name_deletes_nothing(planted):
    """The in-process rollbacks hand their candidate straight to the undo, so the name gets the same
    gate the cover uses (`_checked_names`) at the point of the delete too — `ip link delete` may never
    be handed a value that is not one interface token.

    IT IS NOW THE TYPE THAT SAYS SO, which is stronger than the check it replaces: such a record does
    not become a `Candidate` at all, so there is no object for `_vlan_claim` to be asked about and no
    path — boot, route or restore — on which the name can reach the delete."""
    with pytest.raises(provision.RecordUnknown) as caught:
        provision._checked_candidate({"iface": planted, "vlan": True})

    assert "not an interface name" in str(caught.value)


@pytest.mark.parametrize("recorded,expected", [
    (True, provision.LINK_PRESENT),        # the only observation the superseded bool ever proved
    (False, provision.LINK_UNKNOWN),       # it had already folded "could not tell" into False
    (None, provision.LINK_UNKNOWN),        # `null` claims nothing at all
])
def test_the_superseded_prior_state_bool_is_read_only_as_a_bool(recorded, expected):
    """The prior probe is the other licence to delete, and it can never be granted by this field: a
    record from the older format reads as PRESENT or UNKNOWN, both of which refuse."""
    prior = provision._checked_candidate({"iface": "eth0.9", "link_existed": recorded}).prior

    assert prior == expected
    assert prior != provision.LINK_ABSENT, "an upgraded record licensed deleting a link"


@pytest.mark.parametrize("recorded", ["false", "", 1, 0, [True]])
def test_a_superseded_bool_that_is_not_a_bool_makes_the_whole_record_unreadable(recorded):
    """...and anything that is not a bool is not that field either. A truthy string used to be read as
    PRESENT, reporting a prior state the record never claimed; now the record does not decode, so it
    authorises nothing and is retained for repair — which is the same refusal, said once."""
    with pytest.raises(provision.RecordUnknown) as caught:
        provision._checked_candidate({"iface": "eth0.9", "link_existed": recorded})

    assert "not an answer either way" in str(caught.value)


def test_a_vlan_the_panel_really_created_is_still_removed(settings, stub_xray):
    """The gate is not a refusal to work: the record the panel writes — a real `True`, a VLAN-shaped
    name, a prior probe that proved the link absent — still settles by deleting the link."""
    net = _HostNet(links=("eth0", "eth0.2", "eth0.9"))
    state = _state(settings, stub_xray, net)
    _pending(state, vlan=True)                        # exactly what `provision_candidate` records

    stored = _boot(settings, state, provision.SURVIVOR_KEY)

    assert net.deleted_links() == ["eth0.9"] and "eth0.9" not in net.links
    assert stored[UNDO_KEY] == "" and stored[provision.SURVIVOR_KEY] == ""


def test_a_record_whose_interface_is_not_text_is_no_licence_to_pair_it_as_text():
    """The same record read for its ADDRESSES, and the contract this file used to pin the other way
    round: the pairs were taken as `str(value or "")`, so a record holding `{"iface": 9}` produced the
    survivor `("9", ...)` — an interface that cannot exist, written into the ledger whose entries are
    what the ruleset names and what the drain proves gone. Coercion is not a repair: what the record
    meant is unknown, so reading it says so — and the pairing now takes decoded records only, so the
    refusal happens before there is anything to pair."""
    with pytest.raises(provision.RecordUnknown) as caught:
        provision._checked_candidate({"iface": 9, "addr4": "192.168.10.2/24", "addr6": ""})

    assert "not text" in str(caught.value)


# --- ...and the record of the segment it went BACK to -------------------------------------------
#
# The delete's fourth conjunct is a COMPARISON: a candidate may only be removed when it is not the
# interface the recovery pass restored. That interface arrives out of `managed_segment_iface` as
# whatever text the store holds, and the comparison was a bare `!=` — which answers "different" for
# every value that is not an interface name at all. So `"eth0.9 eth0.2"`, the shape a hand-edited
# database or a truncated write leaves, compared unequal to the candidate `eth0.9`; that conjunct
# passed, and the rest were all legitimately true (a real `vlan: true`, a VLAN-shaped name, a prior
# probe proving absence, the link present now). `ip link delete eth0.9` followed, against the very
# interface the operator's segment had just been put back on.
#
# The danger was never that the malformed value would be USED as a name. It is that an unreadable
# record READS AS PERMISSION when the only question asked of it is whether it differs. So both halves
# are pinned: the value is checked for its shape where the record is read, and the comparison at the
# site that acts on it refuses to answer "different" about a value whose shape it cannot confirm.

NOT_ONE_INTERFACE_NAME = [
    "eth0.9 eth0.2",                    # the richer value: two names where one was written
    "eth0.9\teth0.2",
    "e" * 16,                           # past IFNAMSIZ — what a truncating write leaves
    "eth0.9;ip link delete eth0.2",
]

DELETABLE = {"iface": "eth0.9", "addr4": "192.168.10.2/24", "addr6": "", "vlan": True,
             "link_state": provision.LINK_ABSENT}


@pytest.mark.parametrize("recorded", NOT_ONE_INTERFACE_NAME)
def test_a_restored_interface_that_is_not_one_name_deletes_nothing(settings, stub_xray, recorded,
                                                                  caplog):
    """PRODUCT-CRITICAL, end to end. Every other conjunct of the delete precondition is true, so the
    ownership record naming the restored segment is the whole guard — and a value nothing can read
    as an interface name is not a licence, it is an unreadable record: nothing is removed, nothing is
    recorded, the record stays pending and the enforcement goes on covering the candidate."""
    net = _HostNet(links=("eth0", "eth0.2", "eth0.9"), addrs={"eth0.9": ["192.168.10.2/24"]})
    state = _state(settings, stub_xray, net)
    state.store.set_setting("managed_segment_iface", recorded)
    _pending(state)                                  # eth0.9, a real VLAN claim, prior absent

    with caplog.at_level(logging.ERROR, logger="pi_gw_panel"):
        stored = _boot(settings, state, provision.SURVIVOR_KEY)

    assert net.deleted_links() == [], f"{recorded!r} authorised an ip link delete"
    assert net.deleted_addrs() == []
    assert "eth0.9" in net.links and net.addrs["eth0.9"] == {"192.168.10.2/24"}
    assert stored[UNDO_KEY], "the record the coverage rests on was cleared away"
    assert "not an interface name" in caplog.text        # reported, not silent
    # NEITHER REMOVED NOR RECORDED, the same as a store that would not answer at all: the undo does
    # not go on to write a survivor ledger from a record it could not read.
    assert stored[provision.SURVIVOR_KEY] == ""
    # ...and the interface that was left up is still covered, from that retained record.
    after = _bare_store()
    after.set_setting(UNDO_KEY, stored[UNDO_KEY])
    assert provision.pending_candidate_ifaces(after) == ["eth0.9"]


@pytest.mark.parametrize("recorded", NOT_ONE_INTERFACE_NAME)
def test_the_ownership_read_is_unreadable_for_a_segment_it_cannot_name(recorded):
    """The read itself, which is where such a value stops being anything an undo may act on. It is
    checked against the same expression every cover source goes through, and the answer is the one a
    store fault gets: unreadable, with the reason, and no interface and no address handed back for
    something else to compare against."""
    store = _bare_store()
    store.set_setting("managed_segment_iface", recorded)
    store.set_setting("managed_segment_addr4", "192.168.10.2/24")

    unreadable, iface, addr4, addr6 = provision._read_managed_state(store)

    assert unreadable and "not an interface name" in unreadable[0]
    assert (iface, addr4, addr6) == ("", "", ""), "a value nothing could read came back as content"


def test_the_ownership_read_still_answers_for_the_records_the_panel_writes():
    """...and the readable ones are unchanged: one interface name, both addresses verbatim, and a
    blank scalar is a blank answer rather than a refusal."""
    store = _bare_store()
    store.set_setting("managed_segment_iface", " eth0.2 ")
    store.set_setting("managed_segment_addr4", "192.168.10.2/24")

    assert provision._read_managed_state(store) == ([], "eth0.2", "192.168.10.2/24", "")
    assert provision._read_managed_state(_bare_store()) == ([], "", "", "")


def test_a_restored_interface_the_store_will_not_answer_for_deletes_nothing(settings, stub_xray,
                                                                           monkeypatch):
    """The other way the same record fails to say which interface is in service. A store fault and a
    value that is not text are one answer here — "we cannot tell" — and neither may be spent as "the
    candidate is something else"."""
    net = _HostNet(links=("eth0", "eth0.2", "eth0.9"))
    state = _state(settings, stub_xray, net)
    _pending(state)
    original = state.store.get_setting
    monkeypatch.setattr(state.store, "get_setting",
                        lambda key: 0.9 if key == "managed_segment_iface" else original(key))

    outcome = provision.undo_provision_candidate(state, provision._checked_candidate(DELETABLE))

    assert net.deleted_links() == [] and "eth0.9" in net.links
    assert outcome.unresolved, "an undo that could not read the restored segment settled anyway"
    state.close()


@pytest.mark.parametrize("restored", NOT_ONE_INTERFACE_NAME + [0.9, ["eth0.2"], None])
def test_the_delete_needs_a_PROVEN_difference_and_not_an_inequality(settings, stub_xray, restored):
    """THE CONJUNCT ITSELF, asked at the site that acts on it and not through the read above.

    The read is where such a record is refused in production; this is why refusing it there is not the
    whole fix. `iface != restored` is true of every value that names no interface, so the precondition
    that is supposed to protect the interface in service was the one that waved the delete through.
    Only a difference between two interface NAMES is a difference here."""
    net = _HostNet(links=("eth0", "eth0.2", "eth0.9"))
    state = _state(settings, stub_xray, net)

    outcome, deleted = provision._undo_candidate_link(
        state, provision._checked_candidate(DELETABLE),
        {"iface": restored, "addr4": "", "addr6": ""}, net._run)

    assert deleted is False and net.deleted_links() == []
    assert "eth0.9" in net.links
    assert outcome.unresolved, "the undo reported nothing to retry and nothing to repair"
    assert outcome.actions and "eth0.9" in outcome.actions[0]
    state.close()


@pytest.mark.parametrize("restored,removes", [("eth0.2", True), ("", True), ("eth0.9", False)])
def test_a_restored_interface_that_reads_permits_exactly_what_it_did_before(settings, stub_xray,
                                                                           restored, removes):
    """The gate is not a refusal to work. A real other interface and an unrecorded one both leave the
    candidate deletable, exactly as before — an unset scalar is a legitimate "the panel claims no
    segment interface" — and a record naming the candidate ITSELF still stops the delete silently,
    because that interface is the one now in service."""
    net = _HostNet(links=("eth0", "eth0.2", "eth0.9"))
    state = _state(settings, stub_xray, net)

    outcome, deleted = provision._undo_candidate_link(
        state, provision._checked_candidate(DELETABLE),
        {"iface": restored, "addr4": "", "addr6": ""}, net._run)

    assert deleted is removes and net.deleted_links() == (["eth0.9"] if removes else [])
    assert outcome.unresolved == [] and (outcome.actions != []) is removes
    state.close()


def test_the_genuine_candidate_deletion_still_settles_over_a_recorded_segment(settings, stub_xray):
    """The same thing end to end, over an ownership record that names a real restored interface: the
    orphan goes, the pending record is cleared, and the segment the panel went back to is untouched."""
    net = _HostNet(links=("eth0", "eth0.2", "eth0.9"), addrs={"eth0.2": ["192.168.10.2/24"]})
    state = _state(settings, stub_xray, net)
    state.store.set_setting("managed_segment_iface", "eth0.2")
    state.store.set_setting("managed_segment_addr4", "192.168.10.2/24")
    _pending(state)

    stored = _boot(settings, state, provision.SURVIVOR_KEY)

    assert net.deleted_links() == ["eth0.9"] and "eth0.9" not in net.links
    assert net.deleted_addrs() == [] and net.addrs["eth0.2"] == {"192.168.10.2/24"}
    assert stored[UNDO_KEY] == "" and stored[provision.SURVIVOR_KEY] == ""
    assert stored["managed_segment_iface"] == "eth0.2"


# --- ...and what it does with an ADDRESS: names it, and nothing else -------------------------
#
# The link half of this discipline shipped first, and the address half never worked: the record is
# written before the pass runs a single host command, so its addresses are PREDICTIONS, and an
# undo that trusted them ran `ip addr del` against whatever the prediction named. On a gateway
# that is the segment's own address, and the segment goes down with it. Two successive attempts to
# derive ownership instead — a probe recorded in the candidate, then a second one journalled by the
# pass — each kept a path where a record that was never written, or was lost, read as proof that
# the panel had installed the address.
#
# So the undo no longer deletes an address at all. It names the addresses, the interface they are
# on, and the fact that it left them, and the operator decides. That is the behaviour the tests
# below pin, from both callers and from the boot path.

PRE_ADDRESSED = {"eth1": ["192.168.10.2/24"]}   # the operator's own leg, addressed by the host


def _retarget(state, iface="eth1"):
    """Record the candidate for a change that moves the segment onto `iface`, the way both
    callers do — through the real recorder, so the prior-state probes are the real ones."""
    candidate = provision.provision_candidate(state, {"segment_iface": iface})
    provision.record_provision_candidate(state.store, candidate)
    return candidate


def test_a_crash_before_provisioning_never_deletes_a_pre_existing_address(settings, stub_xray,
                                                                         caplog):
    """PRODUCT-CRITICAL. The operator's own interface already carries the segment address, and the
    panel dies after recording the candidate and before provisioning anything. The undo has
    touched nothing, so there is nothing of its own to reclaim — and the address it would remove
    is the one keeping the segment reachable."""
    net = _HostNet(links=("eth0", "eth0.2", "eth1"), addrs=PRE_ADDRESSED)
    state = _state(settings, stub_xray, net)
    _retarget(state)                  # ...and the process dies here, before `host_provision`

    with caplog.at_level(logging.WARNING, logger="pi_gw_panel"):
        stored = _boot(settings, state)

    assert net.deleted_addrs() == [], "the undo deleted an address the host put there"
    assert net.addrs["eth1"] == {"192.168.10.2/24"}
    assert "192.168.10.2/24" in caplog.text              # reported, not silently skipped
    assert stored[UNDO_KEY] == ""    # recorded as present: no retry can turn that into proof


# --- the same question, asked of the PRODUCTION runner ---------------------------------------
#
# Every test above injects a `_run` seam, which is the layer ABOVE the code that turns a host
# answer into a decision. The one below fakes `subprocess.run` and NOTHING else: the linux
# backend's own runner, the candidate recorder and the undo are all the production path, unpatched
# — so an `ip addr del` reintroduced anywhere along it is caught here too, not only in the tests
# that model the host themselves.


class _SubprocessBoundary:
    """The only fake: `subprocess.run`. `ip addr show` reports the address as being on the
    interface, which is what the host looks like when the operator's own leg carries it."""

    def __init__(self, iface: str, addr: str):
        self.iface, self.addr = iface, addr
        self.cmds: list[list[str]] = []

    def __call__(self, cmd, **kw):
        self.cmds.append(list(cmd))
        out = ""
        if "addr" in cmd and "show" in cmd:
            out = (f"2: {self.iface}    inet {self.addr} scope global {self.iface}\n"
                   f"       valid_lft forever preferred_lft forever\n")
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def deleted_addrs(self) -> list[list[str]]:
        return [cmd for cmd in self.cmds if "addr" in cmd and "del" in cmd]


class _ProdSeamNet(DryRunBackend):
    """A backend whose host seam IS the production runner, so the chain under test runs from
    `provision_candidate` all the way down to `subprocess.run` with nothing substituted."""

    _run = staticmethod(linux._run)


def test_the_production_path_deletes_no_address_and_names_the_one_it_left(settings, stub_xray,
                                                                         monkeypatch):
    """PRODUCT-CRITICAL. The operator's leg carries the segment address and the change is undone.
    Whatever the panel recorded on the way in, nothing on this path may issue `ip addr del`: the
    address it would remove is the one the gateway is reached on. It is reported instead."""
    net = _ProdSeamNet()
    state = _state(settings, stub_xray, net)
    boundary = _SubprocessBoundary("eth1", "192.168.10.2/24")
    monkeypatch.setattr(subprocess, "run", boundary)

    candidate = provision.provision_candidate(state, {"segment_iface": "eth1"})
    provision.record_provision_candidate(state.store, candidate)
    outcome = provision.undo_provision_candidate(state, candidate)

    assert boundary.deleted_addrs() == [], "the undo deleted an address it cannot own"
    assert any("192.168.10.2/24" in line and "eth1" in line for line in outcome.actions)
    assert outcome.unresolved == []          # nothing will ever delete it: no point retrying
    state.close()


def test_an_early_failure_removes_nothing_the_pass_never_installed(settings, stub_xray):
    """The in-process half of the same defect. Both callers record the candidate outside their
    transaction and can fail before the pass installs anything — `installed` still names what the
    panel owned before — and the rollback then runs the undo against a host it never touched."""
    net = _HostNet(links=("eth0", "eth0.2", "eth1"), addrs=PRE_ADDRESSED)
    state = _state(settings, stub_xray, net)
    state.store.set_setting("managed_segment_iface", "eth0.2")     # what the panel owned before
    state.store.set_setting("managed_segment_addr4", "192.168.10.2/24")
    candidate = _retarget(state)
    installed = provision.managed_host_state(state.store)          # the pass added nothing to it

    outcome = provision.undo_provision_candidate(state, candidate, installed)

    assert net.deleted_addrs() == []
    assert net.addrs["eth1"] == {"192.168.10.2/24"}
    assert any("192.168.10.2/24" in line for line in outcome.actions)   # named, not swallowed
    assert outcome.unresolved == []
    state.close()


LEGACY_RECORDS = {
    # Written before any address state was recorded at all.
    "no_addr_state": {"iface": "eth1", "addr4": "192.168.10.2/24", "addr6": "", "vlan": False,
                      "link_state": provision.LINK_PRESENT},
    # Written by the release that recorded a per-address probe and deleted on it. The key is gone;
    # a record still carrying it must be read for its addresses and not for its verdict.
    "with_addr_state": {"iface": "eth1", "addr4": "192.168.10.2/24", "addr6": "", "vlan": False,
                        "link_state": provision.LINK_PRESENT,
                        "addr_state": {"192.168.10.2/24": provision.LINK_ABSENT}},
}


@pytest.mark.parametrize("shape", sorted(LEGACY_RECORDS))
def test_a_record_from_a_previous_release_is_read_and_deletes_no_address(settings, stub_xray,
                                                                        caplog, shape):
    """A pending record survives the upgrade that changes what the undo does with it, so both
    older shapes have to be readable — and neither may delete. The one carrying `addr_state`
    matters most: that field said "the panel installed this, remove it", and honouring it now
    would delete an address on exactly the reasoning this release abandoned."""
    net = _HostNet(links=("eth0", "eth0.2", "eth1"), addrs=PRE_ADDRESSED)
    state = _state(settings, stub_xray, net)
    state.store.set_setting(UNDO_KEY, json.dumps(LEGACY_RECORDS[shape]))

    with caplog.at_level(logging.WARNING, logger="pi_gw_panel"):
        stored = _boot(settings, state)      # asserts /api/health answers: it did not raise

    assert net.deleted_addrs() == []
    assert net.addrs["eth1"] == {"192.168.10.2/24"}
    assert "192.168.10.2/24" in caplog.text and "eth1" in caplog.text
    assert stored[UNDO_KEY] == ""


def test_an_address_the_pass_really_installed_is_left_in_place_and_reported(settings, stub_xray,
                                                                           caplog):
    """THE DECISION, on the case that used to justify deleting. The interface was bare, the pass
    installed the address and died, so this is the one shape where the panel's claim looks strong
    — and it is still not deleted, because the record that makes it look strong is the record a
    restart or an eviction can lose, and then the same delete lands on an operator-owned address.
    The address stays, and the operator is told which one, on which interface, and by whom."""
    net = _HostNet(links=("eth0", "eth0.2", "eth1"))       # eth1 is bare: nothing on it yet
    state = _state(settings, stub_xray, net)
    _retarget(state)
    net.addrs["eth1"] = {"192.168.10.2/24"}                # ...the pass installed it, then died

    with caplog.at_level(logging.WARNING, logger="pi_gw_panel"):
        stored = _boot(settings, state)

    assert net.deleted_addrs() == [], "the undo deleted an address again"
    assert net.addrs["eth1"] == {"192.168.10.2/24"}, "the address was taken off the host"
    assert "192.168.10.2/24" in caplog.text and "eth1" in caplog.text   # named
    assert "left in place" in caplog.text                               # ...and said to be left
    assert stored[UNDO_KEY] == ""


def test_a_kept_link_still_gets_its_candidate_addresses_reported(settings, stub_xray, caplog):
    """A link the panel may not delete is not the end of the undo. The pass can have installed an
    address on a VLAN that was already there: that address survives the rollback, sits outside the
    restored ownership ledger, and is invisible to every later pass — so stopping as soon as the
    link is kept leaves the operator with no mention of it at all."""
    net = _HostNet(links=("eth0", "eth0.2", "eth0.9"), addrs={"eth0.9": ["192.168.10.2/24"]})
    state = _state(settings, stub_xray, net)
    _pending(state, link_state=provision.LINK_PRESENT)   # the VLAN was the host's, the address ours

    with caplog.at_level(logging.WARNING, logger="pi_gw_panel"):
        stored = _boot(settings, state)

    assert net.deleted_links() == []                     # the link is not the panel's to remove
    assert "eth0.9" in net.links
    assert net.deleted_addrs() == []
    assert net.addrs["eth0.9"] == {"192.168.10.2/24"}
    assert "cannot prove it created" in caplog.text      # both halves reported
    assert "192.168.10.2/24" in caplog.text and "left in place" in caplog.text
    assert stored[UNDO_KEY] == ""


# --- the addresses the pass READ BACK, which are not proof either ---------------------------
#
# `installed` is the ownership keys as the pass left them, and those are written BEFORE
# `ip addr replace` for the same reason the candidate is written before the pass: so a rollback
# can still find what to remove. Their presence therefore proves the pass MEANT to install the
# address, never that the interface was not already carrying it. They are still worth reading,
# because a dynamic (`auto`/PD) v6 is resolved inside the pass and appears in no candidate at all,
# so they are the only place that address is ever named — and naming it is now the whole job.

AUTO_ULA = "fd00:aabb:ccdd:2::/64"
AUTO_ADDR6 = "fd00:aabb:ccdd:2::1/64"      # what the pass resolves that prefix to, on its own


def _auto_v6_restore(state, iface="eth1"):
    """A restore document that runs IPv6 in `auto` mode and retargets the segment to `iface`.

    The v6 address is knowable only inside the pass — `auto` resolves to a delegation or, as here,
    to the persistent ULA fallback — so `provision_candidate` records no v6 whatsoever and the
    ownership keys are the only place it ever appears.
    """
    state.store.set_setting("ipv6_enabled", "1")
    state.store.set_setting("segment_ip6", "auto")
    state.store.set_setting("ula_prefix6", AUTO_ULA)
    document = backup_mod.export_state(state.store)
    document["settings"]["segment_iface"] = iface
    return document


def test_a_dynamic_v6_the_host_already_carried_is_never_deleted(settings, stub_xray):
    """PRODUCT-CRITICAL. The operator's own leg already carries both segment addresses, the
    restore retargets onto it, and the pass's `ip addr replace` changes nothing. Reading the keys
    it wrote as ownership deletes the addresses that leg was reachable on."""
    net = _GuardFailsOnce(links=("eth0", "eth0.2", "eth1"),
                          addrs={"eth1": ["192.168.10.2/24", AUTO_ADDR6]})
    state = _state(settings, stub_xray, net)

    result = restore_backup(state, _auto_v6_restore(state))

    assert result.ok is False
    assert ["ip", "-6", "addr", "replace", AUTO_ADDR6, "dev", "eth1"] in net.cmds  # it went there
    assert net.deleted_addrs() == [], "the undo deleted an address the host put there"
    assert net.addrs["eth1"] == {"192.168.10.2/24", AUTO_ADDR6}
    assert AUTO_ADDR6 in result.error and "left in place" in result.error
    state.close()


def test_a_dynamic_v6_the_pass_installed_is_named_in_the_failure(settings, stub_xray):
    """The address only the ownership keys ever name, on the case where the pass really did install
    it: `eth1` is bare beforehand. It is left on the host like every other address — and it is
    named, on its interface, in the error the caller returns, because a v6 the candidate cannot
    predict is otherwise the one leftover nothing anywhere mentions."""
    net = _GuardFailsOnce(links=("eth0", "eth0.2", "eth1"))        # bare: nothing on it yet
    state = _state(settings, stub_xray, net)

    result = restore_backup(state, _auto_v6_restore(state))

    assert result.ok is False
    assert ["ip", "-6", "addr", "replace", AUTO_ADDR6, "dev", "eth1"] in net.cmds
    assert net.deleted_addrs() == []
    assert net.addrs["eth1"] == {"192.168.10.2/24", AUTO_ADDR6}    # both still on the host
    assert AUTO_ADDR6 in result.error and "eth1" in result.error
    assert state.store.get_setting(UNDO_KEY) == ""
    state.close()


# --- the candidate a pass that will not provision must never record --------------------------

def test_an_interrupted_change_with_segment_management_off_records_no_candidate(settings,
                                                                                stub_xray):
    """With `manage_segment` off `host_provision` installs nothing — it CLEARS — so a candidate
    recorded against it names host state the panel is not about to create, and undoing it at boot
    removes state the pass that follows deliberately will not put back."""
    net = _HostNet(links=("eth0", "eth0.2", "eth1"), addrs=PRE_ADDRESSED)
    state = _state(settings, stub_xray, net)
    state.store.set_setting("manage_segment", "0")

    assert _retarget(state) == {}          # `PUT /api/network` cannot edit the key: the store's
    stored = _boot(settings, state)        # value is the projected one

    assert stored[UNDO_KEY] == ""
    assert net.deleted_addrs() == []
    assert net.addrs["eth1"] == {"192.168.10.2/24"}


def test_the_candidate_reads_the_manage_segment_the_change_projects(settings, stub_xray):
    """PROJECTED, not stored. A restore document imports `manage_segment` like any other
    allowlisted setting — it is in the restore's candidate key list — so the pass that follows is
    gated on what the document says, while the store still holds the value about to be replaced.
    """
    net = _HostNet(links=("eth0", "eth0.2", "eth1"), addrs=PRE_ADDRESSED)
    state = _state(settings, stub_xray, net)

    state.store.set_setting("manage_segment", "1")        # on now, off once the document lands
    assert provision.provision_candidate(
        state, {"segment_iface": "eth1", "manage_segment": "0"}) == {}
    state.store.set_setting("manage_segment", "0")        # off now, on once the document lands
    assert provision.provision_candidate(
        state, {"segment_iface": "eth1", "manage_segment": "1"})["iface"] == "eth1"
    state.close()


# --- the second caller ----------------------------------------------------------------------

class _GuardFailsOnce(_HostNet):
    """Fails the fail-closed guard the restore installs INSIDE its transaction — the last step
    before it commits, and the one that leaves the candidate host state stranded."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.guards = 0

    def apply_guard(self, plan):
        self.guards += 1
        if self.guards == 2:
            return NetResult(ok=False, error="nft denied")
        return super().apply_guard(plan)


def test_a_failed_restore_that_moved_the_segment_leaves_no_orphan(settings, stub_xray):
    """`restore_backup` runs `host_provision` inside `state.store.transaction()` and, on failure,
    only re-runs it — which reconciles the RESTORED interface. `segment_iface` is in the
    restorable allowlist, so a restore that retargets the segment and then fails stranded exactly
    the orphan `PUT /api/network` already cleans up. Same ledger, same undo."""
    net = _GuardFailsOnce(links=("eth0", "eth0.2"))
    state = _state(settings, stub_xray, net)
    document = backup_mod.export_state(state.store)
    document["settings"]["segment_iface"] = "eth0.9"

    result = restore_backup(state, document)

    assert result.ok is False
    assert state.store.get_setting("segment_iface") in (None, "", "eth0.2")   # intent rolled back
    assert ["ip", "link", "add", "link", "eth0", "name", "eth0.9",
            "type", "vlan", "id", "9"] in net.cmds                # the candidate really was made
    assert "eth0.9" in net.deleted_links(), \
        "the restored interface was left on the host with nothing recording it"
    assert "eth0.9" not in net.links
    assert "eth0.9" in result.error                               # reported, not silent
    assert state.store.get_setting(UNDO_KEY) == ""
    state.close()


def test_a_restore_that_succeeds_leaves_nothing_pointing_at_an_undo(settings, stub_xray):
    """The record is only a recovery aid: a committed restore must not leave one behind for the
    next boot to act on."""
    net = _HostNet(links=("eth0", "eth0.2"))
    state = _state(settings, stub_xray, net)
    document = backup_mod.export_state(state.store)
    document["settings"]["segment_iface"] = "eth0.9"

    result = restore_backup(state, document)

    assert result.ok is True
    assert "eth0.9" in net.links                                  # the new segment is up
    assert net.deleted_links() == []                              # ...and was not undone
    assert state.store.get_setting(UNDO_KEY) == ""
    state.close()


def test_a_sparse_document_records_the_interface_the_restore_will_actually_create(settings,
                                                                                 stub_xray):
    """The ledger has to name the PROJECTED segment, not the one the document happens to mention.

    A backup is routinely sparse — `export_state` carries only the settings rows that exist — and a
    restore is not: it DELETEs the allowlisted settings, so an omitted `segment_iface` does not mean
    "keep the current override", it means "fall back to the runtime default". Here the box runs an
    override (`eth0.7`, already on the host) and the document omits the key, so `host_provision`
    creates the default `eth0.2` — an interface that exists nowhere else in this scenario. Naming
    the document's view instead of the projected one recorded `eth0.7`, and the VLAN the pass
    actually made was left on the host with nothing pointing at it.
    """
    net = _GuardFailsOnce(links=("eth0", "eth0.7"))     # eth0.2 is NOT on the host yet
    state = _state(settings, stub_xray, net)
    state.store.set_setting("segment_iface", "eth0.7")            # the current override
    document = backup_mod.export_state(state.store)
    document["settings"].pop("segment_iface")                     # ...which the backup omits
    assert settings.segment_iface == "eth0.2", "the runtime fallback this projects onto"

    result = restore_backup(state, document)

    assert result.ok is False
    assert ["ip", "link", "add", "link", "eth0", "name", "eth0.2",
            "type", "vlan", "id", "2"] in net.cmds       # the pass really did create the default
    assert "eth0.2" in net.deleted_links(), \
        "the ledger named the document's interface, so the one actually created was orphaned"
    assert "eth0.2" not in net.links
    assert "eth0.7" in net.links, "the interface the restore went back to was deleted instead"
    assert state.store.get_setting(UNDO_KEY) == ""
    state.close()


# --- what SURVIVES the undo, and what keeps enforcing it -------------------------------------
#
# The undo's decision not to remove something is the common case, not the exception: a candidate
# link the panel cannot prove it created is left alone, a deletion the kernel refuses leaves the
# link up, and an address is never deleted at all. Every one of those ends with an interface that
# may be up carrying the segment address the rolled-back change put there — and the caller's very
# next act is to install the RESTORED guard, rendered from a store that names the interface it went
# back to and nothing else. The interface the change created was then live, addressed, and outside
# the kill-switch drop: the direct-WAN bypass, reached through the recovery path.
#
# So the surviving pairs are recorded where that decision is made, every store-derived render covers
# them, and they leave only when the host proves them gone. The pending-undo record still clears on
# its own rule — "is there work a later pass could finish" — which is a different question.


def _enforced(text: str) -> set:
    """Every interface a rendered ruleset scopes a segment rule to — one name or a set of them."""
    found = set()
    for one, many in re.findall(r'iifname (?:"([^"]+)"|\{([^}]*)\})', text):
        found |= {one} if one else set(re.findall(r'"([^"]+)"', many))
    return found


def _retarget_document(state, iface: str) -> dict:
    """A restore document that moves the segment onto `iface` and changes nothing else."""
    document = backup_mod.export_state(state.store)
    document["settings"]["segment_iface"] = iface
    return document


def test_the_restored_guard_covers_a_pre_existing_candidate_link(settings, stub_xray):
    """PRODUCT-CRITICAL. `eth0.9` was already on the host, so the undo may not delete it — the panel
    cannot prove it created it, and on a live gateway a link it did not create may be the one the
    operator is reached on. The restore put the segment address on it and was then rolled back, so
    it is up, addressed, and no longer anything the store's own plan names. The guard the rollback
    installs has to name it anyway."""
    net = _GuardFailsOnce(links=("eth0", "eth0.2", "eth0.9"))       # eth0.9 pre-dates the panel
    state = _state(settings, stub_xray, net)

    result = restore_backup(state, _retarget_document(state, "eth0.9"))

    assert result.ok is False
    assert net.deleted_links() == [] and "eth0.9" in net.links      # not the panel's to remove
    assert net.addrs["eth0.9"] == {"192.168.10.2/24"}               # ...and it is carrying the segment
    assert _enforced(net.applied[-1]) == {"eth0.2", "eth0.9"}, \
        "the restored guard named only the interface the restore went back to"
    assert state.store.get_setting(UNDO_KEY) == ""                  # no retry can prove ownership
    assert provision._parse_survivors(state.store) == [("eth0.9", "192.168.10.2/24")]
    state.close()


def test_the_restored_guard_covers_a_candidate_link_whose_deletion_was_refused(settings, stub_xray):
    """The other way a candidate survives: the panel DID create the link and owns it, and the
    `ip link delete` is refused. Nothing about the ledger's honesty helps here — the link is still
    up with the address on it, and the ruleset has to cover it exactly as if it were the operator's.
    """
    net = _GuardFailsOnce(links=("eth0", "eth0.2"),
                          refuse=[("ip", "link", "delete", "eth0.9")])
    state = _state(settings, stub_xray, net)

    result = restore_backup(state, _retarget_document(state, "eth0.9"))

    assert result.ok is False
    assert ["ip", "link", "add", "link", "eth0", "name", "eth0.9",
            "type", "vlan", "id", "9"] in net.cmds                  # the pass created it...
    assert "eth0.9" in net.links                                    # ...and it would not go
    assert net.addrs["eth0.9"] == {"192.168.10.2/24"}
    assert _enforced(net.applied[-1]) == {"eth0.2", "eth0.9"}
    assert state.store.get_setting(UNDO_KEY), "an unsettled undo must survive for the next pass"
    state.close()


def test_a_deleted_candidate_link_is_dropped_from_the_cover_it_never_needed(settings, stub_xray):
    """The undo's one deletion settles the enforcement too. A link that goes takes every address on
    it with it, so there is nothing left for a ruleset to cover — and covering it would be the start
    of a set that only ever grows."""
    net = _GuardFailsOnce(links=("eth0", "eth0.2"))
    state = _state(settings, stub_xray, net)

    result = restore_backup(state, _retarget_document(state, "eth0.9"))

    assert result.ok is False
    assert "eth0.9" in net.deleted_links() and "eth0.9" not in net.links
    assert provision._parse_survivors(state.store) == []
    assert _enforced(net.applied[-1]) == {"eth0.2"}
    state.close()


def _survivor_left_by_a_rollback(settings, stub_xray, **host):
    """A gateway whose rolled-back restore left `eth0.9` up, addressed, and covered."""
    net = _GuardFailsOnce(links=("eth0", "eth0.2", "eth0.9"), **host)
    state = _state(settings, stub_xray, net)
    assert restore_backup(state, _retarget_document(state, "eth0.9")).ok is False
    assert provision._parse_survivors(state.store) == [("eth0.9", "192.168.10.2/24")]
    assert _enforced(net.applied[-1]) == {"eth0.2", "eth0.9"}
    return net, state


def _drains_one_boot_later(settings, stub_xray, net, state):
    """The drain, read in the order boot actually performs it. Returns the first boot's settings.

    THE GUARD IS RENDERED BEFORE THE PASS THAT DRAINS, and deliberately: it now goes on before
    anything turns forwarding on, which is ahead of `host_provision` and therefore ahead of
    `drain_enforcement_cover`. So the boot that drains the ledger still installs the WIDER ruleset it
    rendered a moment earlier, and the one after it narrows. Over-covering an interface that is gone
    costs nothing — nft matches `iifname` by name, so the rule simply never matches — while the
    reverse order would render the guard from a cover that had already been shrunk by probes taken
    after it. The property this pins is that the set is still finite: it drains, on its own, without
    the operator doing anything a second time.
    """
    stored = _boot(settings, state, provision.SURVIVOR_KEY)
    assert stored[provision.SURVIVOR_KEY] == "", "the ledger did not drain on the boot that probed"
    assert _enforced(net.applied[-1]) == {"eth0.2", "eth0.9"}, \
        "the boot that drains rendered its guard from a cover its own probes had already shrunk"

    _boot(settings, _state(settings, stub_xray, net), provision.SURVIVOR_KEY)

    assert _enforced(net.applied[-1]) == {"eth0.2"}, "the cover never narrowed at all"
    return stored


def test_a_candidate_the_host_says_is_gone_leaves_the_cover(settings, stub_xray):
    """THE PROPERTY THAT KEEPS THE COVER FINITE. The operator removes the interface the rollback
    reported; the next pass asks the host, gets an explicit not-found, and the ruleset narrows back
    to the configured interface alone. Without this the set of names in the ruleset only grows."""
    net, state = _survivor_left_by_a_rollback(settings, stub_xray)
    net.links.discard("eth0.9")                      # the operator removed it, as reported
    net.addrs.pop("eth0.9", None)

    _drains_one_boot_later(settings, stub_xray, net, state)


def test_a_candidate_whose_address_is_off_it_leaves_the_cover(settings, stub_xray):
    """The same drain for an interface that is NOT going away — the operator's own leg, which the
    panel will never delete. What takes it out of service is the address coming off, and the host is
    asked about exactly that, so the cover drains without anything having to remove a link."""
    net, state = _survivor_left_by_a_rollback(settings, stub_xray)
    net.addrs["eth0.9"] = set()                      # the address was removed by hand; link stays

    _drains_one_boot_later(settings, stub_xray, net, state)

    assert "eth0.9" in net.links                     # still there, still not the panel's


def test_a_candidate_the_host_cannot_answer_for_stays_covered(settings, stub_xray):
    """THE THREE-VALUED PROBE, USED AS THREE VALUES. `ip link show` and `ip addr show` fail without
    an explicit not-found — a refusal, a netlink error, the runner's time limit — which proves
    nothing about the interface. "Not proven absent" is not absent: the interface may be up right
    now carrying the segment, so it stays covered and the ruleset stays wide."""
    net, state = _survivor_left_by_a_rollback(settings, stub_xray)
    net.silent.add("eth0.9")                         # the host stops answering about it

    stored = _boot(settings, state, provision.SURVIVOR_KEY)

    assert _enforced(net.applied[-1]) == {"eth0.2", "eth0.9"}, \
        "an unanswerable probe narrowed the ruleset off an interface that may be live"
    assert stored[provision.SURVIVOR_KEY] == "eth0.9 192.168.10.2/24"


# --- ...and when the ledger write itself is what fails ----------------------------------------
#
# Everything above rests on one assumption: that a decision not to remove something was RECORDED.
# `remember_survivors` used to assume it from the absence of an exception, which a store write does
# not give you — it can raise, and it can also return having kept nothing at all. Either way the
# ledger does not name the interface the undo just left up and addressed, the undo reports nothing
# outstanding, and the caller clears the pending record and renders the restored guard from a store
# that names only the interface it went back to. That is the same direct-WAN bypass the cover exists
# to close, reached through the recovery path, with a ledger claiming otherwise.
#
# So the write is READ BACK, and a failure is handled in the only way that survives a store which
# will not take writes: the undo goes unresolved, so the caller keeps the pending record — already
# on disk, written before the pass began — and that record covers the candidate interface until the
# ledger can. It is a fallback, not a fourth ledger: the moment the write is proven, the undo has
# nothing outstanding, the record clears on its own rule, and the ledger alone answers for the
# interface, drainable by the host exactly as before.


def _write_refused(monkeypatch, store, key: str, how: str):
    """Make `set_setting` fail for one durable record, the two ways a store write can fail silently.

    The record AND its write-ahead journal (`provision.write_set` keeps the complete expected content
    in one before replacing the other), because what is modelled here is a store that will not take
    THIS record at all. Refusing only half of it models something different and much narrower — a
    partial write, where the journal is exactly what keeps the entries covered — and that is worth its
    own test rather than being smuggled in here.
    """
    original = store.set_setting
    refused = {key, provision._journal_key(key)}

    def failing(name, value):
        if name in refused:
            if how == "raises":
                raise RuntimeError("simulated settings write failure")
            return                                   # "succeeded", kept nothing
        original(name, value)

    monkeypatch.setattr(store, "set_setting", failing)
    return original


@pytest.mark.parametrize("how", ["raises", "no-op"])
def test_a_survivor_write_that_fails_keeps_the_undo_pending_and_the_candidate_covered(
        settings, stub_xray, monkeypatch, how):
    """PRODUCT-CRITICAL. `eth0.9` pre-dates the panel, so the undo may not delete it; the rolled-back
    restore put the segment address on it and it is up carrying it. The ledger write that records
    that is refused — by raising, or by keeping nothing while reporting success — and the guard the
    rollback installs straight afterwards must STILL name the interface.

    Both failures answer the same way because the panel can tell them apart only by reading the key
    back, which is exactly what it now does: a write it cannot prove landed is a write that did not.
    """
    net = _GuardFailsOnce(links=("eth0", "eth0.2", "eth0.9"))
    state = _state(settings, stub_xray, net)
    _write_refused(monkeypatch, state.store, provision.SURVIVOR_KEY, how)

    result = restore_backup(state, _retarget_document(state, "eth0.9"))

    assert result.ok is False
    assert "eth0.9" in net.links and net.addrs["eth0.9"] == {"192.168.10.2/24"}   # live, addressed
    assert provision._parse_survivors(state.store) == []          # the ledger really is empty...
    assert _enforced(net.applied[-1]) == {"eth0.2", "eth0.9"}, \
        "the guard was rendered without the live candidate: a failed ledger write reopened the bypass"
    # ...and the undo says so, so the record that carries the cover meanwhile is not thrown away.
    assert state.store.get_setting(UNDO_KEY), "the pending record was cleared over a lost survivor"
    assert provision.pending_candidate_ifaces(state.store) == ["eth0.9"]
    assert "could not be recorded" in result.error and "eth0.9" in result.error
    state.close()


def test_a_recorded_survivor_needs_no_fallback_and_clears_the_pending_record(settings, stub_xray):
    """The unchanged path, pinned against the fix: when the write IS provable, nothing about the
    undo's report or the record's lifetime changes. The ledger names the pair, the undo has nothing
    outstanding, and the pending record clears on its own rule — so the cover has exactly one source
    for that interface and no second set of books to keep."""
    _net, state = _survivor_left_by_a_rollback(settings, stub_xray)

    assert state.store.get_setting(UNDO_KEY) == ""                # cleared: nothing outstanding
    assert provision.pending_candidate_ifaces(state.store) == []  # so the fallback contributes none
    assert provision._parse_survivors(state.store) == [("eth0.9", "192.168.10.2/24")]
    assert provision.enforcement_cover(state.store, "eth0.2").names == ["eth0.9"]
    state.close()


def test_the_retried_write_settles_the_undo_and_the_cover_then_drains(settings, stub_xray,
                                                                     monkeypatch):
    """THE PROPERTY THAT KEEPS THE FALLBACK FINITE, end to end. The store recovers, the next pass
    retries the undo it kept — that is what keeping the record is for — and the write lands. From
    there the interface is covered by the ledger alone, so the host can still take it out: the
    operator removes the address, the drain gets its explicit not-found, and the cover empties. A
    fallback that outlived the write would keep re-adding the interface and nothing could ever
    narrow off it."""
    net = _GuardFailsOnce(links=("eth0", "eth0.2", "eth0.9"))
    state = _state(settings, stub_xray, net)
    original = _write_refused(monkeypatch, state.store, provision.SURVIVOR_KEY, "raises")
    assert restore_backup(state, _retarget_document(state, "eth0.9")).ok is False
    assert state.store.get_setting(UNDO_KEY) and provision._parse_survivors(state.store) == []
    assert provision.enforcement_cover(state.store, "eth0.2").names == ["eth0.9"], \
        "with the ledger empty, the retained record is the only thing covering the interface"

    monkeypatch.setattr(state.store, "set_setting", original)   # the store takes writes again
    outcome = provision.resume_pending_provision_undo(state)     # what the next pass does

    assert outcome.unresolved == []                          # settled now
    assert provision._parse_survivors(state.store) == [("eth0.9", "192.168.10.2/24")]
    assert state.store.get_setting(UNDO_KEY) == ""           # ...so the fallback is out of the way
    assert provision.enforcement_cover(state.store, "eth0.2").names == ["eth0.9"]

    net.addrs["eth0.9"] = set()                              # the operator removes it, as reported
    provision.drain_enforcement_cover(state.store, net._run)

    assert provision.enforcement_cover(state.store, "eth0.2").names == [], \
        "the cover kept naming an interface the host proved is no longer carrying the segment"
    state.close()


# --- ...and when the write lands only PARTLY, or the clear does not land at all ----------------
#
# Everything above is about a write that plainly failed. A settings value holding a SET has a third
# outcome, and it is the one a delta check cannot see: the write lands and the record comes back
# holding some of what was in it. One entry per line, so any truncation keeps the newest and drops
# the oldest — which is precisely the entry nothing will ask for again. Verifying that the pairs the
# caller just handed over came back therefore passes every time, and the older interface, still up
# and still carrying the address a previous rollback left on it, is uncovered permanently.
#
# So the complete expected content is journalled under its own key BEFORE the record is replaced,
# verified afterwards in full, and the journal is cleared only once the record is proven to hold
# everything. What that buys is not a better report: while the record is short the journal still names
# the whole set, and the cover reads the two as one, so the older interface STAYS COVERED.


def _keeps_only_the_last_line(monkeypatch, store, key: str):
    """A store whose write keeps only the LAST line of the record it is given.

    The partial write, in the shape a set record actually takes: one value, one entry per line, so a
    truncation anywhere keeps the newest entries and loses the oldest. Deliberately NOT applied to the
    journal — this models a store that mangles one write, not one that refuses a whole record (see
    `_write_refused`), and the journal is the half that has to survive it.
    """
    original = store.set_setting

    def writing(name, value):
        if name == key:
            lines = [line for line in value.splitlines() if line.strip()]
            value = lines[-1] if lines else value
        original(name, value)

    monkeypatch.setattr(store, "set_setting", writing)


OLDER_SURVIVOR = ("eth0.7", "192.168.8.2/24")


def _gateway_with_an_older_survivor(settings, stub_xray, monkeypatch):
    """A gateway whose cover already names `eth0.7` from an EARLIER rollback, about to take another.

    Two rollbacks is the ordinary case, not a corner: the ledger is additive precisely because a
    second one does not settle the first one's leftovers.
    """
    net = _GuardFailsOnce(links=("eth0", "eth0.2", "eth0.7", "eth0.9"),
                          addrs={"eth0.7": [OLDER_SURVIVOR[1]]})
    state = _state(settings, stub_xray, net)
    state.store.set_setting(provision.SURVIVOR_KEY, " ".join(OLDER_SURVIVOR))
    _keeps_only_the_last_line(monkeypatch, state.store, provision.SURVIVOR_KEY)
    return net, state


def test_a_partial_survivor_write_is_a_failure_and_the_older_interface_stays_covered(
        settings, stub_xray, monkeypatch):
    """PRODUCT-CRITICAL. `eth0.7` is up carrying the segment address an earlier rollback left on it,
    and is in the ledger. This rollback adds `eth0.9`, the record comes back holding `eth0.9` alone,
    and checking only the pair just requested says that worked.

    Two things have to be true afterwards. The write is REPORTED as a failure, so the undo stays
    pending — and `eth0.7` is still covered, which the report alone would not give: the pending record
    names only the current candidate, so a ledger that lost `eth0.7` uncovers it for good.
    """
    net, state = _gateway_with_an_older_survivor(settings, stub_xray, monkeypatch)

    result = restore_backup(state, _retarget_document(state, "eth0.9"))

    assert result.ok is False
    assert net.addrs["eth0.7"] == {OLDER_SURVIVOR[1]} and "eth0.7" in net.links   # live, addressed
    # The record really is short — this is not a test of a store that behaved.
    assert state.store.get_setting(provision.SURVIVOR_KEY) == "eth0.9 192.168.10.2/24"
    assert "could not be recorded" in result.error, \
        "a write that dropped an older survivor was reported as having worked"
    assert state.store.get_setting(UNDO_KEY), "the pending record was cleared over a lost survivor"
    # ...and the older interface is covered anyway, by the journal the replace could not truncate.
    assert set(provision._parse_survivors(state.store)) == {OLDER_SURVIVOR,
                                                            ("eth0.9", "192.168.10.2/24")}
    assert _enforced(net.applied[-1]) == {"eth0.2", "eth0.7", "eth0.9"}, \
        "the guard was rendered without an interface a partial write dropped from the ledger"
    state.close()


def test_a_journalled_survivor_still_drains_when_the_host_proves_it_gone(settings, stub_xray,
                                                                        monkeypatch):
    """The journal may not become a second ledger nothing can empty. The store recovers, the write
    that was short lands in full, and from there the pair drains on the host's own answer exactly as
    an ordinary one does."""
    net, state = _gateway_with_an_older_survivor(settings, stub_xray, monkeypatch)
    assert restore_backup(state, _retarget_document(state, "eth0.9")).ok is False

    monkeypatch.undo()                                       # the store stops mangling writes
    outcome = provision.resume_pending_provision_undo(state)  # the retry the kept record buys

    assert outcome.unresolved == []                          # settled: the whole set landed
    assert set(provision._parse_survivors(state.store)) == {OLDER_SURVIVOR,
                                                            ("eth0.9", "192.168.10.2/24")}
    assert state.store.get_setting(provision._journal_key(provision.SURVIVOR_KEY)) == "", \
        "the journal outlived the write it was covering for"

    net.addrs["eth0.7"] = set()                              # the operator removes both, as reported
    net.addrs["eth0.9"] = set()
    provision.drain_enforcement_cover(state.store, net._run)

    assert provision._parse_survivors(state.store) == []
    assert provision.enforcement_cover(state.store, "eth0.2").names == []
    state.close()


# --- the clear that is not a deletion ---------------------------------------------------------
#
# The pending record is the cover's fallback source, so what it costs to leave one behind is an
# interface named in every ruleset for ever — and, once that name is reused for something else,
# unrelated traffic pulled into the segment's tproxy redirect. Clearing it was `set_setting(key, "")`
# and a `try`/`except`, which takes a non-raising write as proof of deletion; a store that quietly
# keeps nothing leaves the previous JSON exactly where it was.
#
# So the clear no longer depends on the blanking. It writes a TERMINAL form — a record marked
# resolved, which every reader ignores — and PROVES that, then blanks best-effort on top. Whatever
# becomes of the blank write, what is left either reads as settled or is reported.


def _blanking_is_a_noop(monkeypatch, store):
    """A store whose write of an EMPTY value keeps nothing, silently: the deletion that does not
    happen. Every other write behaves, which is what makes this a test of the clear and not of a
    store that is simply broken."""
    original = store.set_setting

    def writing(name, value):
        if name == UNDO_KEY and value == "":
            return                               # "deleted", and the old record is still there
        original(name, value)

    monkeypatch.setattr(store, "set_setting", writing)


def _assert_nothing_pending(state):
    """The record is settled: it names no interface to cover, and no later pass acts on it."""
    assert provision.pending_candidate_ifaces(state.store) == [], \
        "a settled undo record was still naming a candidate interface for the enforcement to cover"
    assert provision.enforcement_cover(state.store, "eth0.2").known, \
        "a settled undo record was left in a state the cover cannot read"
    outcome = provision.resume_pending_provision_undo(state)
    assert outcome.actions == [] and outcome.unresolved == [], \
        "a settled undo record was undone again by the next pass"


def test_a_clear_whose_blanking_never_lands_leaves_no_candidate_behind_after_a_restore(
        settings, stub_xray, monkeypatch):
    """The restore's COMMIT path. The change lands, so nothing is left to undo — and the record that
    says so cannot be blanked. What survives has to be the terminal form, not the JSON naming
    `eth0.9`, or the ruleset covers that interface until someone notices."""
    net = _HostNet(links=("eth0", "eth0.2"))
    state = _state(settings, stub_xray, net)
    _blanking_is_a_noop(monkeypatch, state.store)

    result = restore_backup(state, _retarget_document(state, "eth0.9"))

    assert result.ok is True and "eth0.9" in net.links       # the new segment really came up
    assert state.store.get_setting(UNDO_KEY) == provision.RESOLVED_UNDO
    _assert_nothing_pending(state)
    state.close()


def test_a_clear_whose_blanking_never_lands_leaves_no_candidate_behind_after_a_rollback(
        settings, stub_xray, monkeypatch):
    """The restore's ROLLBACK path, where the record has done its job: the candidate link was the
    panel's, the undo deleted it, and there is nothing outstanding. The interface is GONE, so a
    record still naming it is the case the auditor's "reused interface name" costs most — a later
    `eth0.9` created for something else is inside the segment's redirect."""
    net = _GuardFailsOnce(links=("eth0", "eth0.2"))
    state = _state(settings, stub_xray, net)
    _blanking_is_a_noop(monkeypatch, state.store)

    result = restore_backup(state, _retarget_document(state, "eth0.9"))

    assert result.ok is False
    assert "eth0.9" in net.deleted_links() and "eth0.9" not in net.links
    assert state.store.get_setting(UNDO_KEY) == provision.RESOLVED_UNDO
    _assert_nothing_pending(state)
    assert _enforced(net.applied[-1]) == {"eth0.2"}, \
        "the guard covered an interface the undo had already removed from the host"
    state.close()


class _GuardFailsOnDemand(_HostNet):
    """A host whose fail-closed guard starts working and stops when the test says so — so the app can
    boot and log in normally and the failure lands inside the request under test."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.fail_guard = False

    def apply_guard(self, plan):
        if self.fail_guard:
            return NetResult(ok=False, error="nft denied")
        return super().apply_guard(plan)


@pytest.mark.parametrize("outcome", ["commit", "rollback"])
def test_a_clear_whose_blanking_never_lands_leaves_no_candidate_behind_on_the_route(
        settings, stub_xray, monkeypatch, outcome):
    """The other caller, on both of its completion paths. `PUT /api/network` clears the record in two
    places — after the transaction commits and after the rollback finishes — and a clear that cannot
    prove itself has to hold in both."""
    net = _GuardFailsOnDemand(links=("eth0", "eth0.2"))
    state = _state(settings, stub_xray, net)
    client = TestClient(create_app(settings, state=state))
    headers = {"X-CSRF-Token": _login(client)}
    net.fail_guard = outcome == "rollback"
    _blanking_is_a_noop(monkeypatch, state.store)

    response = client.put("/api/network", json={"segment_iface": "eth0.9"}, headers=headers)

    assert response.status_code == (502 if outcome == "rollback" else 200)
    assert state.store.get_setting(UNDO_KEY) == provision.RESOLVED_UNDO
    _assert_nothing_pending(state)


def test_a_clear_whose_blanking_never_lands_leaves_no_candidate_behind_at_boot(settings, stub_xray,
                                                                              monkeypatch):
    """The boot path's two clears: the one that discards a record it cannot use, and the one that
    settles a finished undo. Neither may leave a live-looking record — and a record already in its
    terminal form must not be re-reported on every boot afterwards, which is the other half of a
    reader that ignores it."""
    net = _HostNet(links=("eth0", "eth0.2", "eth0.9"))
    state = _state(settings, stub_xray, net)
    state.store.set_setting(UNDO_KEY, '{"vlan": true}')      # names no interface: unusable
    _blanking_is_a_noop(monkeypatch, state.store)

    assert provision.resume_pending_provision_undo(state).actions == []
    assert state.store.get_setting(UNDO_KEY) == provision.RESOLVED_UNDO
    _assert_nothing_pending(state)

    _pending(state)                                          # ...and now a real one, which settles
    assert provision.resume_pending_provision_undo(state).unresolved == []
    assert "eth0.9" in net.deleted_links()
    assert state.store.get_setting(UNDO_KEY) == provision.RESOLVED_UNDO
    _assert_nothing_pending(state)
    state.close()


# --- ...and the terminal form is the EXACT record, not anything that mentions it ----------------
#
# The clear's terminal record is the one thing every reader below may ignore, so what counts as one
# decides whether an interface goes on being covered. It was read as `if candidate.get("resolved")`
# — truthiness over a value that arrives out of JSON — so `{"resolved": "false", "iface": "eth0.9"}`
# was TERMINAL: a non-empty string is truthy, and a record still naming a candidate interface came
# back as a known-empty cover, licensing a narrow, while eth0.9 may have been up carrying the segment.
# A hand-edited database, a foreign backup document or a half-decoded write is all it takes.
#
# So the test is identity with `True` over the exact key set the clear writes. Everything else that
# mentions `resolved` is a record the panel never wrote: it is not read as settled, it is not read as
# a candidate an undo may act on either, and it is RETAINED — unknown, refusing, covered.

HALF_SETTLED = [
    {"resolved": "false", "iface": "eth0.9"},    # the finding: truthy, and still naming an interface
    {"resolved": "true", "iface": "eth0.9"},     # the same string, the other way round
    {"resolved": 1, "iface": "eth0.9"},          # `1 == True`, so equality alone would accept it
    {"resolved": True, "iface": "eth0.9"},       # settled AND naming a candidate: not both
    {"resolved": False, "iface": "eth0.9"},      # nothing the panel writes says this
]


@pytest.mark.parametrize("record", HALF_SETTLED)
def test_a_record_that_only_claims_to_be_settled_keeps_covering_its_interface(record):
    """The finding, at the level it is decided. Not settled, so the cover still names eth0.9; not a
    candidate either, so nothing may act on it."""
    store = _bare_store()
    store.set_setting(UNDO_KEY, json.dumps(record))       # set verbatim: no writer produces this

    pending = provision._pending_candidate(store)
    cover = provision.pending_candidate_state(store)

    assert cover.names == ["eth0.9"], "a half-settled record stopped covering the interface it names"
    assert cover.known is False and cover.may_narrow is False, \
        "a record the panel never wrote licensed narrowing the enforcement off its own candidate"
    assert "settled form" in cover.why()
    assert pending.candidate is None, "a half-settled record was handed to the undo as a candidate"
    assert pending.unusable == "", "a record naming a live interface was marked for discarding"


def test_a_record_read_by_equality_would_be_settled_by_the_number_one(caplog):
    """`1 == True` in Python, and a dict compares its values with `==`, so equality with the terminal
    record — or with `True` — accepts `{"resolved": 1}`, which nothing here wrote. Identity plus the
    key set is what makes the accepted shape exactly one shape."""
    store = _bare_store()
    store.set_setting(UNDO_KEY, json.dumps({"resolved": 1}))

    cover = provision.pending_candidate_state(store)

    assert cover.known is False, "the number 1 was read as the resolution the clear writes"
    assert cover.may_narrow is False
    assert provision._pending_candidate(store).unusable == "", "it was marked for discarding"


def test_the_settled_record_is_still_settled_and_still_ignored(settings, stub_xray):
    """The other side of the same line: the exact form the clear writes stays invisible to every
    reader, and is not undone again on the boot after it."""
    net = _HostNet(links=("eth0", "eth0.2", "eth0.9"))
    state = _state(settings, stub_xray, net)
    state.store.set_setting(UNDO_KEY, provision.RESOLVED_UNDO)

    assert json.loads(provision.RESOLVED_UNDO) == {"resolved": True}      # nothing else in it
    _assert_nothing_pending(state)                       # no cover, no re-undo, nothing reported
    assert net.deleted_links() == []
    state.close()


def test_a_boot_never_acts_on_a_record_that_only_claims_to_be_settled(settings, stub_xray, caplog):
    """PRODUCT-CRITICAL, end to end. eth0.9 is on the host and the record half-says it is settled, so
    the boot may neither remove it nor forget it: the record is what the enforcement is covering the
    interface from until an operator repairs it."""
    net = _HostNet(links=("eth0", "eth0.2", "eth0.9"), addrs={"eth0.9": ["192.168.10.2/24"]})
    state = _state(settings, stub_xray, net)
    planted = json.dumps({"resolved": "false", "iface": "eth0.9", "addr4": "192.168.10.2/24",
                          "addr6": "", "vlan": True, "link_state": provision.LINK_ABSENT})
    state.store.set_setting(UNDO_KEY, planted)
    assert provision.pending_candidate_ifaces(state.store) == ["eth0.9"]   # covered going in

    with caplog.at_level(logging.WARNING, logger="pi_gw_panel"):
        stored = _boot(settings, state)

    assert net.deleted_links() == [] and net.deleted_addrs() == []
    assert "eth0.9" in net.links and net.addrs["eth0.9"] == {"192.168.10.2/24"}
    assert stored[UNDO_KEY] == planted, "the record the coverage rests on was cleared or rewritten"
    # ...and the pass refused to install a ruleset it could not prove covers eth0.9, rather than
    # rendering one that omits it: the boot's own fail-closed path, reached through this record.
    assert "could not be established" in caplog.text
    assert _enforced("\n".join(net.applied)) != {"eth0.2"} or net.applied == [], \
        "a ruleset naming only the restored interface was installed while eth0.9 was uncovered"


# --- unknown, from every record the cover is built out of -------------------------------------


@pytest.mark.parametrize("key", [provision.SURVIVOR_KEY, provision.LINK_KEY, provision.STALE_KEY,
                                "managed_segment_iface", "pending_provision_undo"])
def test_a_cover_source_that_cannot_be_read_is_never_read_as_empty(settings, stub_xray, monkeypatch,
                                                                  key):
    """Every record the cover is derived from, asked the same question: does a store that will not
    answer for you come back as "there is nothing to cover"?

    It used to, for the one the auditor found, and the shape was general — a `try`/`except` returning
    `[]`, or an `or ""` in front of a `.splitlines()`. So the answer is pinned for all five: the names
    are whatever could be read, `known` is False, and `may_narrow` — the only thing that licenses
    taking an interface out of the ruleset — is False whatever the names say.
    """
    net = _HostNet(links=("eth0", "eth0.2", "eth0.9"))
    state = _state(settings, stub_xray, net)
    _pending(state)
    state.store.set_setting(provision.SURVIVOR_KEY, "eth0.7 192.168.8.2/24")
    original = state.store.get_setting

    def reading(name):
        if name == key:
            raise RuntimeError("simulated settings read failure")
        return original(name)

    monkeypatch.setattr(state.store, "get_setting", reading)
    cover = provision.enforcement_cover(state.store, "eth0.2")

    assert cover.known is False and cover.why()
    assert cover.may_narrow is False, "an unreadable record licensed narrowing the enforcement"
    with pytest.raises(TypeError):          # ...and it cannot be spent as a truth value either
        bool(cover)
    assert cover != [], "an unknown cover compared equal to an empty one"
    state.close()


# --- the record's SHAPE, checked once, where it is read ----------------------------------------
#
# Four rounds hardened the readers of this one record and each ended in the same place: a value of the
# wrong TYPE was made to look like a value of the right one. The round before this one resolved a type
# confusion by COERCING the value to text, and that is what these tests exist to forbid, because
# coercion makes malformed data look valid:
#
#   * `str(0.9)` is `"0.9"`. It passes `_checked_names` (`_IFACE_RE` allows digits and a dot), it
#     parses as VLAN 9, and with `vlan: true`, a prior `absent` and a link of that name on the host it
#     reaches `ip link delete 0.9` — the delete is the one irreversible thing left in the undo.
#   * `str([1])` is `"[1]"`. It is reported to the operator as an address, written into the survivor
#     ledger as a survivor, and then proven absent by that ledger's very first probe: the interface
#     leaves the enforcement cover while a real segment address may still be on it, and the pending
#     record that was covering it has been cleared as settled.
#
# So every field of the record is checked for type and form at the ONE boundary where the record is
# read, and a record that fails is unknown: covered, refusing, retained, never a licence to act.

NOT_AN_INTERFACE = [0.9, 9, [1], ["eth0.9"], {"name": "eth0.9"}, None, True]


def test_a_boot_never_deletes_a_link_named_by_a_number(settings, stub_xray):
    """PRODUCT-CRITICAL, end to end. Everything else in the record says delete — a real `vlan: true`,
    a prior probe that proved the link absent, a link of that name on the host now — so the JSON number
    `0.9` is the whole difference between a repair and `ip link delete 0.9`. It is not text, so the
    record cannot be read as one the panel wrote: nothing is deleted, the record is KEPT, and the
    interface it names goes on being covered."""
    net = _HostNet(links=("eth0", "eth0.2", "0.9"))
    state = _state(settings, stub_xray, net)
    _pending(state, iface=0.9)

    stored = _boot(settings, state)

    assert net.deleted_links() == [], "a link named by a JSON number was deleted"
    assert "0.9" in net.links
    assert stored[UNDO_KEY], "a record that could not be read was cleared away"
    # ...and what the retained record is worth: the cover it feeds is unknown, so nothing narrows the
    # enforcement off whatever that record was meant to name, boot after boot (`_enforcement_plan`).
    kept = _bare_store()
    kept.set_setting(UNDO_KEY, stored[UNDO_KEY])
    cover = provision.pending_candidate_state(kept)
    assert cover.known is False and cover.may_narrow is False, \
        "a record nothing can read licensed narrowing the enforcement"
    assert "not text" in cover.why()


@pytest.mark.parametrize("planted", NOT_AN_INTERFACE + [""])
def test_no_shape_but_one_interface_name_is_read_as_a_candidate(settings, stub_xray, planted):
    """Every shape an `iface` can arrive in and be refused: a number, a list, an object, `null`, a
    bool, and the blank string. None of them may delete a link, and none of them may be handed to an
    undo as a candidate — the record does not say which interface a pass may have touched."""
    net = _HostNet(links=("eth0", "eth0.2", "eth0.9", "0.9"))
    state = _state(settings, stub_xray, net)
    _pending(state, iface=planted)

    stored = _boot(settings, state, provision.SURVIVOR_KEY)

    assert net.deleted_links() == [] and net.deleted_addrs() == []
    assert provision._pending_candidate(state.store).candidate is None, \
        "a record whose interface is not one interface name was handed to an undo"
    assert stored[provision.SURVIVOR_KEY] == "", "a survivor was recorded for an unnamed interface"
    state.close()


@pytest.mark.parametrize("planted", NOT_AN_INTERFACE)
def test_an_iface_that_is_not_one_interface_name_keeps_the_record_and_refuses(planted):
    """...and what a refused record DOES: it is retained, not discarded, and the cover it feeds is
    unknown. Retaining is the whole point — clearing it throws away the coverage for the one interface
    a rolled-back pass may have put the segment on, in the window before the survivor ledger can
    answer for it."""
    store = _bare_store()
    store.set_setting(UNDO_KEY, json.dumps(
        {"iface": planted, "addr4": "192.168.10.2/24", "addr6": "", "vlan": True,
         "link_state": provision.LINK_ABSENT}))

    pending = provision._pending_candidate(store)

    assert pending.candidate is None, "a record nothing can read authorised an undo"
    assert pending.unusable == "", "a record that may be naming a live interface was discarded"
    assert pending.cover.known is False and pending.cover.may_narrow is False
    assert provision.pending_candidate_ifaces(store) == []


def test_a_record_that_conforms_and_names_no_interface_is_the_one_that_is_discarded():
    """THE ONE VERDICT THAT IS STILL `unusable`, and the line it sits on. A blank `iface` is not a
    malformed value: it is text, the type the panel writes, saying there is no candidate — the same
    answer as a record with no `iface` key at all, which is what a pass that provisions nothing
    records. Reading it as unreadable instead would retain it for ever, and a retained unknown cover
    means every store-derived render REFUSES (see `controller._enforcement_plan`), so a record that
    names nothing would hold the segment at no network at all until an operator edited the database.
    """
    store = _bare_store()
    store.set_setting(UNDO_KEY, json.dumps({"iface": "  ", "vlan": True}))

    pending = provision._pending_candidate(store)

    assert pending.candidate is None and "names no candidate interface" in pending.unusable
    assert pending.cover.known is True and pending.cover.names == []


@pytest.mark.parametrize("planted", [[1], {"addr": "192.168.10.2/24"}, 9, True,
                                     "192.168.10.2", "192.168.10.2/24 192.168.10.9/24", "nonsense"])
def test_an_address_that_is_not_one_writes_no_survivor_and_keeps_the_record(settings, stub_xray,
                                                                           planted, caplog):
    """THE SECOND HALF OF THE FINDING. `str([1])` is `"[1]"`, and that string used to be persisted as a
    surviving candidate: the undo then had nothing outstanding, the pending record was cleared as
    settled, and the ledger held a pair the very first probe proves absent — so the interface left the
    cover with a real segment address possibly still on it. A bare address is in this list for the same
    reason: `_probe_addr` matches the whole CIDR token, so a prefix that is missing is a pair the host
    can never confirm.

    The candidate here is one the undo will NOT delete — the record says the panel did not create the
    link — so the address half is the half that runs, which is exactly where the bogus survivor was
    persisted and the pending record then cleared as settled.
    """
    net = _HostNet(links=("eth0", "eth0.2", "eth0.9"), addrs={"eth0.9": ["192.168.10.2/24"]})
    state = _state(settings, stub_xray, net)
    _pending(state, addr4=planted, vlan=False, link_state=provision.LINK_PRESENT)

    with caplog.at_level(logging.ERROR, logger="pi_gw_panel"):
        provision.resume_pending_provision_undo(state)

    assert provision._parse_survivors(state.store) == [], \
        "a survivor that is not an interface and an address was written into the ledger"
    assert state.store.get_setting(UNDO_KEY), "the record that was covering eth0.9 was cleared away"
    assert "neither acted on nor cleared" in caplog.text, "the operator was told nothing"

    stored = _boot(settings, state, provision.SURVIVOR_KEY)   # ...and the same across a whole boot

    assert stored[provision.SURVIVOR_KEY] == "" and stored[UNDO_KEY]
    assert net.deleted_links() == [] and net.deleted_addrs() == []
    # THE PROPERTY, asked of the state the boot leaves behind: eth0.9 is still up carrying the segment
    # address, so it is still an interface the enforcement may not narrow off. Coerced, the bogus
    # survivor went into the ledger, the pending record was cleared as settled, and the drain then
    # proved only that `"[1]"` was absent — leaving the cover empty, KNOWN, and free to narrow.
    after = _bare_store()
    after.set_setting(UNDO_KEY, stored[UNDO_KEY])
    after.set_setting(provision.SURVIVOR_KEY, stored[provision.SURVIVOR_KEY])
    cover = provision.enforcement_cover(after, "eth0.2")
    assert cover.may_narrow is False, "the interface a rolled-back change left addressed was uncovered"
    assert cover.names == ["eth0.9"] and cover.known is False
    assert "eth0.9" in net.links and net.addrs["eth0.9"] == {"192.168.10.2/24"}


def test_a_bogus_survivor_is_never_drained_out_of_the_cover(settings, stub_xray):
    """The same lesson one record along, which is where the damage was going to land: the ledger's
    entries are the names the enforcement carries and the tokens the drain proves gone. An entry that
    is not an interface and an address cannot be answered for by the host — the probe's not-found is
    about the entry, not about whatever it was meant to name — so the ledger is not read as a ledger
    at all: nothing is drained, nothing is shrunk, and the cover stays unknown."""
    store = _bare_store()
    store.set_setting(provision.SURVIVOR_KEY, "eth0.9 [1]")

    kept = provision.drain_enforcement_cover(store, run=_never_asked)

    assert kept == []                                    # nothing was proven gone, so nothing dropped
    assert store.get_setting(provision.SURVIVOR_KEY) == "eth0.9 [1]", "the ledger was shrunk"
    cover = provision.enforcement_cover(store, "eth0.2")
    assert cover.known is False and cover.may_narrow is False, \
        "a ledger nothing can read licensed narrowing the enforcement"
    assert provision.remember_survivors(store, [("eth0.9", "[1]")]) is False, \
        "a survivor that is not an address was accepted into the ledger"


def _never_asked(cmd, **kw):
    raise AssertionError(f"the host was asked about an entry nothing can read: {cmd}")


@pytest.mark.parametrize("record", [
    {"iface": "eth0.9", "vlan": True, "link_state": provision.LINK_ABSENT},      # no address at all
    {"iface": "eth0.9", "addr4": "", "addr6": "", "vlan": True,
     "link_state": provision.LINK_ABSENT},                                      # both blank
])
def test_a_record_with_no_addresses_is_still_the_record_the_panel_writes(settings, stub_xray, record):
    """The legitimate shapes stay legitimate. An absent address field and a blank one both mean "this
    change claimed no address there" — what a pass with IPv6 off records, and what the unarmed
    preflight record a restore writes looks like — so neither may be read as malformed."""
    net = _HostNet(links=("eth0", "eth0.2", "eth0.9"))
    state = _state(settings, stub_xray, net)
    state.store.set_setting(UNDO_KEY, json.dumps(record))

    stored = _boot(settings, state)

    assert net.deleted_links() == ["eth0.9"], "a record the panel writes was refused"
    assert stored[UNDO_KEY] == ""                       # settled, with nothing outstanding
    state.close()


def test_a_well_formed_record_still_authorises_exactly_what_it_did_before(settings, stub_xray):
    """The gate is not a refusal to work. The record `provision_candidate` writes — text interface, a
    real `True`, addresses with prefixes, one of the three probe answers — still deletes the link it
    proves the panel created, still reports the addresses it left behind, and still settles."""
    net = _HostNet(links=("eth0", "eth0.2", "eth1"), addrs=PRE_ADDRESSED)
    state = _state(settings, stub_xray, net)
    candidate = _retarget(state)                        # through the real recorder: the real shapes

    assert candidate == {"iface": "eth1", "addr4": "192.168.10.2/24", "addr6": "",
                         "vlan": False, "link_state": provision.LINK_PRESENT}
    outcome = provision.undo_provision_candidate(state, candidate)

    assert net.deleted_links() == [] and net.deleted_addrs() == []       # eth1 is the operator's
    assert any("192.168.10.2/24" in line and "eth1" in line for line in outcome.actions)
    assert outcome.unresolved == []                                     # nothing left to finish
    assert provision._parse_survivors(state.store) == [("eth1", "192.168.10.2/24")]
    state.close()


def test_a_candidate_the_panel_could_not_read_back_is_never_recorded(settings, stub_xray):
    """The same check on the WRITE side, because a record the readers will refuse to act on is a pass
    with no recovery story — and the only moment that can still be reported instead of discovered on a
    later boot is before the pass runs."""
    net = _HostNet(links=("eth0", "eth0.2"))
    state = _state(settings, stub_xray, net)

    with pytest.raises(RuntimeError, match="would not be readable"):
        provision.record_provision_candidate(state.store, {"iface": 0.9, "vlan": True})

    assert (state.store.get_setting(UNDO_KEY) or "") == ""
    state.close()


# --- the record as a TYPE, not a dict ----------------------------------------------------------
#
# Five rounds hardened this record's readers FIELD BY FIELD and the class survived all five, because
# what was left is not about a field. A JSON object with a repeated key had already lost one of two
# values before any field check ran. `link_state` and `link_existed` each passed alone while
# contradicting each other, and the reader preferred the spelling that authorises a delete. A
# canonicalising address check handed back the ORIGINAL token, so a validated record could name an
# address that IS on the interface and be answered "absent" by the first probe. `resolved` was
# reserved for the clear and checked by nobody validating a live candidate. A blank `iface` was spent
# as "there is no candidate" before the rest of the record was looked at.
#
# So the record is decoded ONCE — duplicate-aware — canonicalised ONCE, checked as a whole, and handed
# to its readers as a `Candidate` they cannot misread. These tests are that contract, from both sides.

DUPLICATED = {
    # The name half: two interfaces, one of which the enforcement would then never cover.
    "iface": '{"iface": "eth0.9", "iface": "eth0.2", "addr4": "", "addr6": "", "vlan": true, '
             '"link_state": "absent"}',
    # The authorisation half: the last value wins, and the last value is the one that deletes.
    "link_state": '{"iface": "eth0.9", "addr4": "", "addr6": "", "vlan": true, '
                  '"link_state": "present", "link_state": "absent"}',
    "vlan": '{"iface": "eth0.9", "addr4": "", "addr6": "", "vlan": false, "vlan": true, '
            '"link_state": "absent"}',
    # ...and the word that would make the whole record invisible to every reader.
    "resolved": '{"resolved": false, "resolved": true}',
}


@pytest.mark.parametrize("key", sorted(DUPLICATED))
def test_a_duplicate_key_is_not_a_record_and_nothing_acts_on_it(key):
    """THE DECODE, and it runs before every check there is. `json.loads` keeps the LAST value for a
    repeated key, so a record saying two things arrived as a record saying one — chosen by position,
    and in each shape here the surviving value is the dangerous one: the interface the cover would then
    never name, the probe answer that licenses `ip link delete`, the claim that authorises it, the word
    that makes the record read as already settled. No writer here can produce a duplicate key; a
    hand-edited database, a merged backup document or a truncated concatenation can. So it is not a
    record — unknown, refusing, RETAINED, exactly as an `iface` nothing can read is."""
    store = _bare_store()
    store.set_setting(UNDO_KEY, DUPLICATED[key])

    pending = provision._pending_candidate(store)

    assert pending.candidate is None, "a record that lost half of itself was handed to an undo"
    assert pending.unusable == "", "a record that may be naming a live interface was discarded"
    assert pending.cover.known is False and pending.cover.may_narrow is False, \
        "a record nothing can read in full licensed narrowing the enforcement"
    assert "more than once" in pending.cover.why()
    assert provision.pending_candidate_ifaces(store) == []


def test_a_duplicated_record_at_boot_deletes_nothing_and_is_kept(settings, stub_xray, caplog):
    """PRODUCT-CRITICAL, end to end. The surviving `link_state` is `absent`, the `vlan` claim is a real
    `true`, the name is VLAN-shaped and the link is on the host — every conjunct of the delete — while
    the value the record ALSO holds says the panel did not create it. Read as one value it deletes an
    interface it does not own."""
    net = _HostNet(links=("eth0", "eth0.2", "eth0.9"), addrs={"eth0.9": ["192.168.10.2/24"]})
    state = _state(settings, stub_xray, net)
    planted = DUPLICATED["link_state"]
    state.store.set_setting(UNDO_KEY, planted)

    with caplog.at_level(logging.ERROR, logger="pi_gw_panel"):
        stored = _boot(settings, state)

    assert net.deleted_links() == [] and net.deleted_addrs() == []
    assert "eth0.9" in net.links and net.addrs["eth0.9"] == {"192.168.10.2/24"}
    assert stored[UNDO_KEY] == planted, "the record was cleared or rewritten"
    assert "neither acted on nor cleared" in caplog.text     # the operator is told once per boot
    state.close()


# The spellings `ipaddress` accepts for one address, none of which `ip addr show` ever prints. Every
# one of them used to be validated and then RECORDED AS WRITTEN, and compared as text against the
# kernel's own spelling on the very next probe.
OTHER_SPELLINGS = {
    "netmask": ("192.168.10.2/255.255.255.0", "192.168.10.2/24"),
    "expanded_v6": ("FD00:1234:5678:0009:0000:0000:0000:0001/64", "fd00:1234:5678:9::1/64"),
}


def _spelled(spelling: str) -> tuple[str, str, str, dict]:
    """`(written, canonical, field, candidate)` for one alternative spelling of a live address.

    The candidate is on a link the panel did NOT create, so the undo's address half is the half that
    runs — which is where the survivor is recorded and where the drain then asks the host about it.
    """
    written, canonical = OTHER_SPELLINGS[spelling]
    field = "addr6" if ":" in canonical else "addr4"
    return written, canonical, field, (
        {"iface": "eth0.9", "addr4": "", "addr6": "", "vlan": False,
         "link_state": provision.LINK_PRESENT} | {field: written})


@pytest.mark.parametrize("spelling", sorted(OTHER_SPELLINGS))
def test_a_non_canonical_address_is_canonicalised_at_the_boundary(settings, stub_xray, spelling):
    """ONE SPELLING, on the way in and on the way out. The check accepted every spelling `ipaddress`
    accepts and handed back the token as written, so the record carried a value the kernel never
    prints — and the probe compares against what the kernel prints."""
    written, canonical, field, candidate = _spelled(spelling)
    state = _state(settings, stub_xray, _HostNet(links=("eth0", "eth0.2", "eth0.9")))

    provision.record_provision_candidate(state.store, candidate)

    assert json.loads(state.store.get_setting(UNDO_KEY))[field] == canonical, \
        "the record was stored in a spelling the probe does not compare"
    decoded = provision._pending_candidate(state.store).candidate
    assert getattr(decoded, field) == canonical, "the decoded record carries another spelling"
    assert provision._checked_addr_text(written, "an address") == canonical
    state.close()


@pytest.mark.parametrize("spelling", sorted(OTHER_SPELLINGS))
def test_a_live_address_spelled_another_way_is_never_falsely_drained(settings, stub_xray, spelling):
    """PRODUCT-CRITICAL, and the harm the spelling did. The address IS on the interface, in the
    kernel's spelling; the record names it in another. Whatever the record carries, the drain's probe
    must answer about the ADDRESS — otherwise the survivor is proven absent by its very first probe,
    eth0.9 leaves the enforcement cover, and the segment address is still on it."""
    _written, canonical, _field, candidate = _spelled(spelling)
    net = _HostNet(links=("eth0", "eth0.2", "eth0.9"), addrs={"eth0.9": [canonical]})
    state = _state(settings, stub_xray, net)

    outcome = provision.undo_provision_candidate(state, candidate)

    assert outcome.unresolved == []                      # the survivor write landed, so it settles
    assert [iface for iface, _ in provision._parse_survivors(state.store)] == ["eth0.9"]
    # THE PROPERTY: nothing is proven gone, so the pair stays and the interface stays covered.
    assert [iface for iface, _ in provision.drain_enforcement_cover(state.store, run=net._run)] \
        == ["eth0.9"], "a live address was drained out of the cover on a spelling mismatch"
    assert provision.enforcement_cover(state.store, "eth0.2").names == ["eth0.9"]
    assert net.deleted_addrs() == [] and net.deleted_links() == []
    assert net.addrs["eth0.9"] == {canonical}, "the address the cover was for left the host"
    state.close()


@pytest.mark.parametrize("spelling", sorted(OTHER_SPELLINGS))
def test_the_probe_answers_about_the_address_and_not_about_its_spelling(spelling):
    """The comparison side, where the mismatch actually lived. `_probe_addr` matched the record's token
    against `ip addr show` as TEXT, so every spelling above answered ABSENT for an address that is
    there — and ABSENT is the one answer that drains a survivor and retires an ownership record. Both
    sides are parsed now, so the answer is about the address."""
    written, canonical = OTHER_SPELLINGS[spelling]
    family = "inet6" if ":" in canonical else "inet"
    listed = (f"2: eth0.9    {family} {canonical} scope global eth0.9\n"
              "       valid_lft forever preferred_lft forever\n")

    assert provision._probe_addr("eth0.9", written,
                                 run=lambda cmd, input=None: listed)[0] == provision.ADDR_PRESENT, \
        "a live address was read as absent because the record spelled it differently"
    # ...and the other direction is untouched: a different address is still ABSENT, prefix included.
    assert provision._probe_addr("eth0.9", written.replace("2/", "9/").replace("1/", "9/"),
                                 run=lambda cmd, input=None: listed)[0] == provision.ADDR_ABSENT


def test_an_owned_address_in_another_spelling_is_never_forgotten_as_absent():
    """The same comparison on the path that is not decoded at all: the ownership records, whose
    addresses reach `_probe_addr` as stored. A refused `ip addr del` asks the host whether the address
    is still there, and only ABSENT licenses dropping the record that would retry the removal — so a
    spelling mismatch there forgets an address that is on the host with nothing left to reclaim it."""
    listed = ("2: eth0.9    inet 192.168.10.2/24 scope global eth0.9\n"
              "       valid_lft forever preferred_lft forever\n")

    def host(cmd, **kw):
        if cmd[1:3] == ["addr", "del"] or cmd[2:4] == ["addr", "del"]:
            raise subprocess.CalledProcessError(2, cmd, stderr="RTNETLINK answers: refused")
        return subprocess.CompletedProcess(cmd, 0, listed, "")

    kept = provision._retire_owned_addr("eth0.9", "192.168.10.2/255.255.255.0", run=host)

    assert kept is not None, "an address still on the host was forgotten as absent"
    assert "192.168.10.2/255.255.255.0 on eth0.9" in kept


def test_a_contradicted_prior_link_state_authorises_no_deletion(settings, stub_xray, caplog):
    """PRODUCT-CRITICAL. `link_state` and the superseded `link_existed` are two spellings of ONE
    observation, and they were validated independently: `{"link_state": "absent", "link_existed":
    true}` passed both while saying the panel both did and did not create the link. The reader then
    preferred `link_state` — the spelling that authorises `ip link delete`. A record that says it twice
    and disagrees with itself is not one the panel wrote: nothing acts on it, and it is kept."""
    net = _HostNet(links=("eth0", "eth0.2", "eth0.9"))
    state = _state(settings, stub_xray, net)
    planted = json.dumps({"iface": "eth0.9", "addr4": "192.168.10.2/24", "addr6": "", "vlan": True,
                          "link_state": provision.LINK_ABSENT, "link_existed": True})
    state.store.set_setting(UNDO_KEY, planted)

    pending = provision._pending_candidate(state.store)
    assert pending.candidate is None and pending.unusable == ""
    assert pending.cover.names == ["eth0.9"] and pending.cover.known is False

    with caplog.at_level(logging.ERROR, logger="pi_gw_panel"):
        stored = _boot(settings, state)

    assert net.deleted_links() == [], "a record that contradicts itself deleted a link"
    assert "eth0.9" in net.links
    assert stored[UNDO_KEY] == planted, "the record the coverage rests on was cleared"
    assert "which observation was taken cannot be read" in caplog.text
    state.close()


RESOLVED_LIVE = [
    {"resolved": True},                                     # the clear's own word, as a live record
    {"resolved": True, "iface": "eth0.9", "vlan": True, "link_state": provision.LINK_ABSENT},
    {"resolved": False, "iface": "eth0.9", "vlan": False, "link_state": provision.LINK_UNKNOWN},
]


@pytest.mark.parametrize("candidate", RESOLVED_LIVE)
def test_a_live_candidate_may_not_claim_to_be_settled(settings, stub_xray, candidate):
    """`resolved` is the CLEAR's word and the validator every writer and reader shares ignored it. So
    `{"resolved": true}` could be written as a pending record — proven, looking to its caller like a
    successful arm, and read by every reader as ALREADY SETTLED: no cover, no undo, and an interface a
    pass is about to create named nowhere. Refused where it can still be reported: the write."""
    net = _HostNet(links=("eth0", "eth0.2"))
    state = _state(settings, stub_xray, net)

    with pytest.raises(RuntimeError, match="only a clear may write that word"):
        provision.record_provision_candidate(state.store, candidate)

    assert (state.store.get_setting(UNDO_KEY) or "") == "", "a live candidate claiming to be settled"
    # ...and the in-process rollbacks, which hand their dict straight to the undo, refuse it too.
    outcome = provision.undo_provision_candidate(state, candidate)
    assert outcome.unresolved and net.deleted_links() == []
    assert "settled undo" in outcome.unresolved[0]
    state.close()


def test_a_blank_interface_in_a_record_that_does_not_conform_is_kept(settings, stub_xray, caplog):
    """THE ORDER, and it was the wrong way round. A blank `iface` was classified as "there is no
    candidate" — discardable, cleared by the next boot — BEFORE the remaining fields were looked at, so
    `{"iface": "", "addr4": [1]}` was cleared as known-empty while conforming to nothing. The discard
    is only ever right for a record that is otherwise exactly what the panel writes, so every other
    field is proven first and this one is retained."""
    net = _HostNet(links=("eth0", "eth0.2"))
    state = _state(settings, stub_xray, net)
    planted = json.dumps({"iface": "", "addr4": [1]})
    state.store.set_setting(UNDO_KEY, planted)

    pending = provision._pending_candidate(state.store)
    assert pending.candidate is None
    assert pending.unusable == "", "a record that conforms to nothing was discarded as known-empty"
    assert pending.cover.known is False and pending.cover.may_narrow is False, \
        "a record nothing can read licensed narrowing the enforcement"

    with caplog.at_level(logging.ERROR, logger="pi_gw_panel"):
        stored = _boot(settings, state)

    assert stored[UNDO_KEY] == planted, "the record was cleared instead of kept for repair"
    assert "neither acted on nor cleared" in caplog.text
    state.close()


@pytest.mark.parametrize("record", [
    {"iface": "  ", "vlan": True},                          # blank text: "there is no candidate"
    {"vlan": False, "link_state": provision.LINK_UNKNOWN},   # ...and the same said by omission
    {"iface": "", "addr4": "", "addr6": "", "vlan": False, "link_state": provision.LINK_UNKNOWN},
])
def test_a_conforming_record_that_names_no_interface_is_still_discarded(record):
    """The disclosed discard behaviour, unchanged for the records it was meant for. Reading these as
    unreadable instead would retain them for ever, and a retained unknown cover makes every
    store-derived render REFUSE — so a record that names nothing would hold the segment at no network
    at all until an operator edited the database. That is the over-rejection this must not become."""
    store = _bare_store()
    store.set_setting(UNDO_KEY, json.dumps(record))

    pending = provision._pending_candidate(store)

    assert pending.candidate is None
    assert "names no candidate interface" in pending.unusable
    assert pending.cover.known is True and pending.cover.names == []
    assert pending.cover.may_narrow is True, "a record that says there is nothing to cover refused"


# EVERY SHAPE THE PANEL HAS EVER WRITTEN HERE, because over-rejection is its own defect: a record the
# readers refuse is a cover that stays unknown, and every store-derived render refuses with it — the
# segment held at no network at all until someone edits the database. This module has produced that
# outcome twice from safety machinery, so the shapes are enumerated and proven rather than assumed.
PANEL_WRITES = {
    # `provision_candidate` today: a VLAN the pass is about to create, proven absent beforehand.
    "armed_vlan": {"iface": "eth0.9", "addr4": "192.168.10.2/24", "addr6": "", "vlan": True,
                   "link_state": provision.LINK_ABSENT},
    # ...with IPv6 on, where `addr6` is whatever `host_addr6` resolved for the segment.
    "armed_v6": {"iface": "eth0.9", "addr4": "192.168.10.2/24", "addr6": "fd00:1234:5678:9::1/64",
                 "vlan": True, "link_state": provision.LINK_ABSENT},
    # A retarget onto an interface the operator owns: no VLAN created, the link was already there.
    "retarget": {"iface": "eth1", "addr4": "192.168.10.2/24", "addr6": "", "vlan": False,
                 "link_state": provision.LINK_PRESENT},
    # The probe that would not answer, recorded as such — it refuses the delete for ever.
    "unprobed": {"iface": "eth0.9", "addr4": "192.168.10.2/24", "addr6": "", "vlan": True,
                 "link_state": provision.LINK_UNKNOWN},
    # `controller._unarmed_candidate`: the restore's preflight, before the host has been touched.
    "unarmed_preflight": {"iface": "eth0.9", "addr4": "", "addr6": "", "vlan": False,
                          "link_state": provision.LINK_UNKNOWN, "armed": False},
    # A pass with nothing to record in either address: the keys are simply absent.
    "no_addresses": {"iface": "eth0.9", "vlan": True, "link_state": provision.LINK_ABSENT},
    # The release before `link_state`: a bool that had already folded "could not tell" into False.
    "legacy_link_existed": {"iface": "eth1", "addr4": "192.168.10.2/24", "addr6": "", "vlan": False,
                            "link_existed": True},
    "legacy_link_existed_false": {"iface": "eth0.9", "addr4": "192.168.10.2/24", "addr6": "",
                                  "vlan": True, "link_existed": False},
    # The release that recorded a per-address probe and deleted on it: read for its addresses only.
    "legacy_addr_state": {"iface": "eth1", "addr4": "192.168.10.2/24", "addr6": "", "vlan": False,
                          "link_state": provision.LINK_PRESENT,
                          "addr_state": {"192.168.10.2/24": provision.LINK_ABSENT}},
}


@pytest.mark.parametrize("shape", sorted(PANEL_WRITES))
def test_every_shape_the_panel_writes_still_reads(shape):
    """The whole enumeration, read through the boundary and written through the writer. A pending
    record outlives the release that wrote it — that is the entire reason it exists — so each of these
    must decode into a candidate, name its interface to the cover, and be neither discarded nor
    refused."""
    record = PANEL_WRITES[shape]
    store = _bare_store()
    store.set_setting(UNDO_KEY, json.dumps(record))

    pending = provision._pending_candidate(store)

    assert pending.candidate is not None, f"the panel's own {shape} record was refused"
    assert pending.candidate.iface == record["iface"]
    assert pending.unusable == "", f"the panel's own {shape} record was discarded"
    assert pending.cover.known is True and pending.cover.names == [record["iface"]]
    assert provision.pending_candidate_ifaces(store) == [record["iface"]]
    # ...and the writer accepts it too: one validator, both sides of the record.
    provision.record_provision_candidate(store, record)
    assert json.loads(store.get_setting(UNDO_KEY))["iface"] == record["iface"]
    for extra in ("armed", "addr_state"):           # nothing the panel put there is dropped
        assert (extra in json.loads(store.get_setting(UNDO_KEY))) == (extra in record)


@pytest.mark.parametrize("raw", ["", "   ", provision.RESOLVED_UNDO])
def test_the_settled_shapes_are_still_nothing_at_all(raw):
    """The other two shapes the panel writes: the blank record and the exact terminal form the clear
    writes. Neither is a candidate, neither is reported, and both leave the cover complete and free to
    narrow — the settled state, quietly."""
    store = _bare_store()
    store.set_setting(UNDO_KEY, raw)

    pending = provision._pending_candidate(store)

    assert pending.candidate is None and pending.unusable == ""
    assert pending.cover.known is True and pending.cover.may_narrow is True


def test_the_typed_record_cannot_be_read_as_the_dict_it_came_from():
    """The point of the type. Every defect in this record's history is a reader reaching a raw field
    and deriving a poorer answer from it — `get("vlan")` over JSON making `"false"` an authorisation,
    `str(get("iface") or "")` turning `0.9` into a name `ip link delete` accepted. So there is nothing
    left to reach, and the wrong access fails loudly rather than plausibly."""
    candidate = provision._checked_candidate(PANEL_WRITES["armed_vlan"])

    assert candidate.iface == "eth0.9" and candidate.vlan is True
    assert candidate.prior == provision.LINK_ABSENT
    with pytest.raises(TypeError):
        candidate["vlan"]
    with pytest.raises(TypeError):
        candidate.get("vlan")
    with pytest.raises(TypeError):          # "no candidate" is None, not a falsey record
        bool(candidate)


# --- `installed` may not be flattened before the decode that is supposed to refuse it -----------
#
# `managed_host_state` reads the same three scalars for the undo's `installed`, and documented that it
# need not prove their shape because its one consumer decodes them through `_as_candidate` first. `or
# ""` ran BEFORE that gate and turned every falsey non-text value — `0`, `False`, `[]`, `{}` — into the
# string that means "the panel claims no address here". So the gate was never reached: `installed` is
# the ONLY source of an address the pass resolved for itself (an `auto`/PD /64), that address was left
# out of the orphan report and out of the survivor ledger, and the pending record was cleared over a
# record that had not been read.


@pytest.mark.parametrize("value", [0, False, [], {}])
def test_an_installed_address_that_could_not_be_read_keeps_the_undo_pending(settings, stub_xray,
                                                                           value):
    """PRODUCT-CRITICAL. The undo may not settle over a record it did not read. What it would have
    omitted is the address only `installed` names, on an interface that is still up — so the whole
    record is unreadable, nothing is removed, nothing is recorded, and the pending record stays."""
    net = _HostNet(links=("eth0", "eth0.2", "eth1"), addrs=PRE_ADDRESSED)
    state = _state(settings, stub_xray, net)
    state.store.set_setting("managed_segment_iface", "eth0.2")
    state.store.set_setting("managed_segment_addr4", "192.168.10.2/24")
    candidate = _retarget(state)

    # Read through a store that answers with the value, rather than written into one: a real sqlite
    # column refuses a list outright, and what is under test is the READ, which any store wrapper —
    # a JSON column, a cache, a restore document decoded straight into settings — can present.
    plain = state.store.get_setting
    state.store.get_setting = (
        lambda key: value if key == "managed_segment_addr6" else plain(key))
    installed = provision.managed_host_state(state.store)
    state.store.get_setting = plain
    outcome = provision.undo_provision_candidate(state, candidate, installed)

    assert net.deleted_addrs() == []
    assert outcome.unresolved, "the pending record settled over a record that was not read"
    assert any("not the record the panel writes" in line for line in outcome.actions)
    state.close()
