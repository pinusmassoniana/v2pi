"""The panel-created-VLAN ledger, and what a single-token record does with an entry that is not one.

The link ledger is newline-joined interface names, and that format used to be treated as
self-validating: "an entry here is ONE token, so unlike the pair ledgers there is no half-parsed
form of it to discard". The justification for leaving it permissive was that a mangled entry
over-covers and then fails its own delete. It does neither.

`eth0.2 eth0.9` — what a hand-edited database, a foreign backup document or a truncated write
produces — comes back as ONE name, `known`, and:

  * it covers nothing. nft matches `iifname` by name, so a rule naming `"eth0.2 eth0.9"` matches no
    packet, while `eth0.2` and `eth0.9` — the two interfaces the entry stood for — are named in
    neither the kill-switch drop nor the tproxy redirect;
  * it does not fail its own delete either. `ip link delete "eth0.2 eth0.9"` is answered "does not
    exist", which is the ONE answer that proves absence, so the entry is dropped from the ledger and
    the interfaces it stood for are forgotten for good.

So the record is validated one legal interface token at a time, all-or-nothing, and an entry that
is not one makes the WHOLE record unknown — which every consumer already knows how to handle: the
cover forbids narrowing, the render installs nothing, the retirement paths touch no link and report.
"""
import json
import re
import subprocess

import pytest

from pi_gw_panel.controller import apply_net, stop_net, sync_net
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.net_control import provision
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.net_control.plan import NetPlan
from pi_gw_panel.net_control.provision import LINK_KEY, RecordUnknown
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.state import build_state

MANGLED = "eth0.2 eth0.9"
UNDO_KEY = provision.PROVISION_UNDO_KEY

# Everything that cannot be one interface name, and so names nothing an enforcement rule can match.
ILLEGAL = [MANGLED,                          # whitespace: the finding
           'eth0"; drop',                    # a quote, which would close the render's own string
           "eth0.2\teth0.9",                 # a tab is whitespace too
           "verylonginterfacename"]          # past IFNAMSIZ, so no such device can exist


def _store():
    conn = connect(":memory:")
    init_schema(conn)
    return NodeStore(conn)


def _plant(store, text: str) -> None:
    """The ledger as a hand-edited database / restored document leaves it — set verbatim, not
    through `write_set`, because the entry this is about is one no writer here would produce."""
    store.set_setting(LINK_KEY, text)


class _Host:
    """A runner that records every command and answers `ip link show` for a fixed set of links."""

    def __init__(self, links=("eth0", "eth0.2", "eth0.9")):
        self.links = set(links)
        self.cmds: list[list[str]] = []

    def __call__(self, cmd, input=None):
        self.cmds.append(list(cmd))
        rest = [tok for tok in cmd[1:] if tok not in ("-4", "-6", "-o")]
        if rest[:2] in (["link", "show"], ["addr", "show"]) and rest[-1] not in self.links:
            raise subprocess.CalledProcessError(
                1, cmd, stderr=f'Device "{rest[-1]}" does not exist.')
        if rest[:2] == ["link", "delete"]:
            self.links.discard(rest[-1])
        return subprocess.CompletedProcess(cmd, 0, "", "")


class _Watched(DryRunBackend):
    """A backend that records which of its entry points a caller reached, and with what scope.

    Recording rather than raising, deliberately: `_call_net` turns any exception out of a backend
    into `NetResult(ok=False)`, so a backend that raises makes "refused before the backend" and
    "installed a bogus ruleset" produce the same failed result. What "preserve the live ruleset"
    means for a backend that only ever REPLACES one is that it was not entered at all.
    """

    def __init__(self):
        super().__init__()
        self.entered: list[tuple[str, tuple]] = []

    def apply_tproxy(self, plan):
        self.entered.append(("apply_tproxy", tuple(plan.extra_ifaces)))
        return super().apply_tproxy(plan)

    def apply_guard(self, plan):
        self.entered.append(("apply_guard", tuple(plan.extra_ifaces)))
        return super().apply_guard(plan)

    def teardown(self):
        self.entered.append(("teardown", ()))
        return super().teardown()


def _enforced(text: str) -> set:
    """Every interface a rendered ruleset scopes a segment rule to — one name or a set of them."""
    found = set()
    for one, many in re.findall(r'iifname (?:"([^"]+)"|\{([^}]*)\})', text):
        found |= {one} if one else set(re.findall(r'"([^"]+)"', many))
    return found


# --- the record itself ---------------------------------------------------------------------------


def test_a_two_token_link_entry_makes_the_whole_record_unknown():
    """The finding, at the level it is decided: not one entry skipped, the record unknown."""
    store = _store()
    _plant(store, MANGLED)

    with pytest.raises(RecordUnknown) as raised:
        provision._parse_links(store)

    assert MANGLED in str(raised.value)
    assert "not one interface name" in str(raised.value)


@pytest.mark.parametrize("planted, expected", [
    ("eth0.9", ["eth0.9"]),                      # the upgrade path: a single bare name
    ("eth0.2\neth0.9", ["eth0.2", "eth0.9"]),    # the ordinary multi-entry ledger
    ("  eth0.9  \n\n", ["eth0.9"]),              # blanks and padding are still not entries
    ("", []),                                    # nothing owned, and that is a known answer
])
def test_a_legal_single_token_entry_still_parses(planted, expected):
    """The other half of the contract: everything that IS a name reads exactly as it did."""
    store = _store()
    _plant(store, planted)

    assert provision._parse_links(store) == expected


@pytest.mark.parametrize("planted", ILLEGAL)
def test_nothing_that_cannot_be_an_interface_name_is_read_as_one(planted):
    store = _store()
    _plant(store, planted)

    with pytest.raises(RecordUnknown):
        provision._parse_links(store)


# --- what the consumers do with it ---------------------------------------------------------------


def test_a_mangled_entry_forbids_narrowing_instead_of_naming_a_bogus_interface():
    """The cover is the decision the ledger feeds, and it has to come back as "no answer".

    Under the old read it came back ANSWERED, naming `"eth0.2 eth0.9"`: `known` true, so a pass was
    free to narrow, and the name it carried matched no packet.
    """
    store = _store()
    _plant(store, MANGLED)

    cover = provision.enforcement_cover(store, "eth0.2")

    assert cover.known is False
    assert cover.may_narrow is False
    assert cover.names == []
    assert MANGLED in cover.why()


def test_the_superseded_read_reports_it_too():
    """`superseded_state` decides whether a pass is a move and what the NM drop-in takes over."""
    store = _store()
    _plant(store, MANGLED)

    superseded = provision.superseded_state(store, "eth0.2")

    assert superseded.known is False
    assert superseded.names == []


@pytest.mark.parametrize("call", ["apply_net", "stop_net"])
def test_no_ruleset_is_rendered_while_the_ledger_names_something_that_is_not_an_interface(
        settings, stub_xray, call):
    """PRODUCT-CRITICAL. A ruleset is replaced, not edited, so the alternative to refusing is a
    ruleset whose only segment scope is a name no packet can match — every real interface released
    to the WAN. The backend is not entered at all."""
    settings.xray_bin = stub_xray
    state = build_state(settings, net=_Watched())
    state.dnsmasq = state.pd_client = None
    try:
        _plant(state.store, MANGLED)

        result = (apply_net if call == "apply_net" else stop_net)(
            state.settings, state.net, state.store)

        assert result.ok is False
        assert MANGLED in result.error
        assert state.net.entered == [], "a ruleset was installed from a ledger nobody could read"
        assert state.net.enforcement_status == "error"
        assert state.net.wan_blocked is None
    finally:
        state.close()


def test_a_readable_ledger_still_renders_over_every_link_it_names(settings, stub_xray, monkeypatch):
    """The regression guard for the fix: a ledger of legal names covers all of them."""
    settings.xray_bin = stub_xray
    state = build_state(settings, net=DryRunBackend())
    state.dnsmasq = state.pd_client = None
    try:
        _plant(state.store, "eth0.2\neth0.9")
        state.store.set_setting("active_node_id", "1")
        monkeypatch.setattr(state.supervisor, "status", lambda: {"running": True})

        assert sync_net(state).ok is True
        assert _enforced(state.net.applied[-1]) == {"eth0.2", "eth0.9"}
    finally:
        state.close()


def test_a_mangled_entry_is_not_deleted_and_forgotten():
    """The claim that made the permissive read look safe, tested: the delete does NOT fail.

    `ip link delete "eth0.2 eth0.9"` is answered "does not exist" — proof of absence — so the old
    read dropped the entry and left `eth0.2`/`eth0.9` recorded nowhere. Now no link command runs at
    all and the ledger keeps the entry, so the operator's repair is still there to make.
    """
    store = _store()
    _plant(store, MANGLED)
    host = _Host()

    failures = provision.clear_managed_link(store, run=host)

    assert failures and MANGLED in failures[0]
    assert [cmd for cmd in host.cmds if cmd[:3] == ["ip", "link", "delete"]] == []
    assert store.get_setting(LINK_KEY) == MANGLED


def test_the_retirement_path_reports_it_rather_than_retiring_the_wrong_thing(settings):
    store = _store()
    _plant(store, MANGLED)
    host = _Host()
    plan = NetPlan.from_settings(settings)

    failures = provision.retire_superseded_links(store, plan, run=host)

    assert failures and MANGLED in failures[0]
    assert [cmd for cmd in host.cmds if cmd[:3] == ["ip", "link", "delete"]] == []
    assert store.get_setting(LINK_KEY) == MANGLED


def test_a_pass_configures_nothing_while_the_ledger_cannot_be_read(settings, stub_xray):
    """`host_provision` end to end: the pass declines, the host is untouched, and it reports."""
    settings.xray_bin = stub_xray
    net = DryRunBackend()
    net._run = _Host()                       # the `_run` seam is what makes it the linux path
    state = build_state(settings, net=net)
    state.dnsmasq = state.pd_client = None
    try:
        _plant(state.store, MANGLED)

        result = provision.host_provision(state)

        assert result.ok is False
        assert MANGLED in result.error
        assert [cmd for cmd in net._run.cmds if cmd[:2] == ["ip", "link"]] == []
    finally:
        state.close()


# --- the same rule for the last source that was not following it ---------------------------------
#
# The pending-undo record is the cover's fallback for the window in which the survivor ledger cannot
# yet answer: it names the ONE interface a rolled-back pass may have put the segment on, and it is on
# disk before the pass runs a single command. It was reading its own `iface` straight out of the JSON,
# so `{"iface": "eth0.2 eth0.9"}` produced `Cover(known=True, names=["eth0.2 eth0.9"])` — a complete
# answer, licensing a narrow, over a name no packet can match.
#
# The second half matters as much as the validation: a record like that may NOT be quietly discarded.
# `unusable` records are cleared by the boot resume, and clearing this one throws away the coverage it
# stands for. Unknown, refusing, retained.


def _plant_pending(store, iface: str) -> None:
    """A pending undo record naming `iface` — set verbatim, as a hand-edited database or a foreign
    backup document leaves it, because no writer here would produce the entry this is about."""
    store.set_setting(UNDO_KEY, json.dumps(
        {"iface": iface, "addr4": "192.168.10.2/24", "addr6": "", "vlan": True,
         "link_state": provision.LINK_ABSENT}))


@pytest.mark.parametrize("planted", ILLEGAL)
def test_a_pending_candidate_that_is_not_an_interface_name_is_no_answer(planted):
    """The finding, at the level it is decided: not a name that over-covers, no answer at all."""
    store = _store()
    _plant_pending(store, planted)

    pending = provision.pending_candidate_state(store)

    assert pending.known is False
    assert pending.names == []
    assert pending.may_narrow is False, "a bogus candidate name licensed narrowing the enforcement"
    # The value is quoted into the reason as `repr` — a tab has to reach the operator visibly.
    assert repr(planted) in pending.why()
    assert "not an interface name" in pending.why()
    assert provision.pending_candidate_ifaces(store) == []      # nothing installable came out of it


def test_the_whole_cover_refuses_while_the_pending_candidate_cannot_be_read():
    """The cover is the decision this record feeds, so the unknown has to reach it — and it may not
    be traded for the names the other four sources gave."""
    store = _store()
    _plant_pending(store, MANGLED)
    store.set_setting(provision.SURVIVOR_KEY, "eth0.7 192.168.8.2/24")

    cover = provision.enforcement_cover(store, "eth0.2")

    assert cover.known is False
    assert cover.may_narrow is False
    assert cover.names == ["eth0.7"]          # a render installs what it can; it may not narrow
    assert MANGLED in cover.why()


def test_a_mangled_pending_candidate_is_kept_rather_than_discarded(settings, stub_xray):
    """THE HALF THAT IS NOT THE VALIDATION. `unusable` records — unparseable, not a record, naming no
    interface at all — are cleared at boot, and that is right: they say nothing an undo could act on.
    This one says an interface may be LIVE and then says it in something nothing can enforce on, so
    clearing it would delete the only record of the interface a rolled-back pass may have addressed.
    It is kept, no host command runs against the name, and every store-derived render refuses while
    it is there — which is the operator's cue to repair it.
    """
    settings.xray_bin = stub_xray
    net = _Watched()
    net._run = _Host()                       # the `_run` seam is what makes it the linux path
    state = build_state(settings, net=net)
    state.dnsmasq = state.pd_client = None
    try:
        _plant_pending(state.store, MANGLED)

        outcome = provision.resume_pending_provision_undo(state)

        assert outcome.actions == [] and outcome.unresolved == []
        assert json.loads(state.store.get_setting(UNDO_KEY))["iface"] == MANGLED, \
            "the boot resume discarded the coverage the record stood for"
        assert [cmd for cmd in net._run.cmds if cmd[:2] == ["ip", "link"]] == []

        result = stop_net(state.settings, state.net, state.store)

        assert result.ok is False and MANGLED in result.error
        assert net.entered == [], "a ruleset was installed from a record nobody could read"
    finally:
        state.close()


def test_a_legal_pending_candidate_still_covers_the_interface_it_names(settings, stub_xray):
    """The regression guard for the fix: the fallback source still does its job, all the way into the
    ruleset. A candidate a rolled-back change left up is named in the drop and the redirect."""
    settings.xray_bin = stub_xray
    state = build_state(settings, net=DryRunBackend())
    state.dnsmasq = state.pd_client = None
    try:
        _plant_pending(state.store, "eth0.9")

        pending = provision.pending_candidate_state(state.store)

        assert pending.known is True and pending.names == ["eth0.9"]
        assert provision.pending_candidate_ifaces(state.store) == ["eth0.9"]
        assert provision.enforcement_cover(state.store, "eth0.2").names == ["eth0.9"]

        assert stop_net(state.settings, state.net, state.store).ok is True
        assert _enforced(state.net.applied[-1]) == {"eth0.2", "eth0.9"}
    finally:
        state.close()
