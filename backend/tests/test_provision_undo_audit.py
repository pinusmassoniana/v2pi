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
from pi_gw_panel.net_control import provision
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.net_control.plan import NetResult
from pi_gw_panel.state import build_state

UNDO_KEY = "pending_provision_undo"


class _HostNet(DryRunBackend):
    """A dry-run backend plus the `_run` seam that makes host provisioning take its linux path.

    Models the one thing the undo reasons about — which links are on the host — and speaks like
    iproute2 while doing it: absence is an explicit not-found on stderr, and anything else
    (`silent`) is a command that failed without saying anything about the device.
    """

    def __init__(self, links=("eth0", "eth0.2"), refuse=(), silent=()):
        super().__init__()
        self.cmds: list[list[str]] = []
        self.links = set(links)
        self.refuse = [tuple(cmd) for cmd in refuse]   # command prefixes that fail
        self.silent = set(silent)                      # links `ip link show` cannot answer for

    def _run(self, cmd, **kw):
        self.cmds.append(list(cmd))
        for prefix in self.refuse:
            if tuple(cmd[:len(prefix)]) == prefix:
                raise subprocess.CalledProcessError(1, cmd, stderr="RTNETLINK answers: refused")
        if cmd[:3] == ["ip", "link", "show"]:
            if cmd[3] in self.silent:
                raise subprocess.CalledProcessError(1, cmd)          # no answer about the device
            if cmd[3] not in self.links:
                raise subprocess.CalledProcessError(
                    1, cmd, stderr=f'Device "{cmd[3]}" does not exist.')
        if cmd[:3] == ["ip", "link", "add"]:
            self.links.add(cmd[cmd.index("name") + 1])
        if cmd[:3] == ["ip", "link", "delete"]:
            self.links.discard(cmd[3])
        return ""

    def deleted_links(self) -> list[str]:
        return [cmd[3] for cmd in self.cmds if cmd[:3] == ["ip", "link", "delete"]]

    def deleted_addrs(self) -> list[list[str]]:
        return [cmd for cmd in self.cmds if cmd[:3] in (["ip", "addr", "del"],
                                                        ["ip", "-6", "addr"])]


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
