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
import subprocess

import pytest
from fastapi.testclient import TestClient

from pi_gw_panel import backup as backup_mod
from pi_gw_panel.app import create_app
from pi_gw_panel.controller import restore_backup
from pi_gw_panel.net_control import linux, provision
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.net_control.plan import NetResult
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

    Here the pass creates the segment VLAN, records it in its own ownership ledger, and then dies
    bringing it up — so the address metadata is never written and the record still names that
    interface. Running the undo AFTER the pass reads those empty keys, sees a link its record says
    was not there beforehand, and deletes the very link the pass just created and claimed: the
    ledger then describes a host that no longer matches it, and nothing re-creates the link this
    boot. Running it BEFORE cannot: the pass that follows re-asserts the configured segment.
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
