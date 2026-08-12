"""What `restore_backup` proves before it disconnects the gateway it is replacing.

`PUT /api/network` refuses before it mutates the host: the validation and the candidate record come
first, so a refusal leaves the gateway exactly as it was. The restore did the same two things in the
opposite order — it stopped xray and installed the disconnected guard, and only THEN recorded what
the pass was about to put on the host. A store that would not take that record therefore imported
nothing, applied nothing, and left a previously working gateway disconnected; and the write is
transient by nature (a locked database, a full disk), which is exactly the kind of failure that must
not cost a working tunnel.

So the two phases are ordered: everything that can refuse is proven while the operator's gateway is
still running, and the record is settled again on every exit after that which never starts a pass.
"""
import json
import re
import subprocess

import pytest

from pi_gw_panel import backup as backup_mod
from pi_gw_panel import controller
from pi_gw_panel.controller import apply_node, restore_backup
from pi_gw_panel.models import Node
from pi_gw_panel.net_control import provision
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.net_control.plan import NetResult
from pi_gw_panel.state import build_state

UNDO_KEY = provision.PROVISION_UNDO_KEY


class _Net(DryRunBackend):
    """The dry-run backend plus the `_run` seam that makes provisioning take its linux branch — the
    only way a restore has a candidate to record at all. Thin on purpose: every command succeeds, and
    a `show` for a device that is not on the host answers the way iproute2 does, because that is what
    the candidate link probe reads."""

    def __init__(self, links=("eth0", "eth0.2")):
        super().__init__()
        self.links = set(links)
        self.cmds: list[list[str]] = []

    def _run(self, cmd, **kw):
        self.cmds.append(list(cmd))
        rest = [tok for tok in cmd[1:] if tok not in ("-4", "-6", "-o")]
        if rest[:2] in (["link", "show"], ["addr", "show"]) and rest[-1] not in self.links:
            raise subprocess.CalledProcessError(
                1, cmd, stderr=f'Device "{rest[-1]}" does not exist.')
        if rest[:2] == ["link", "add"]:
            self.links.add(cmd[cmd.index("name") + 1])
        if rest[:2] == ["link", "delete"]:
            self.links.discard(rest[-1])
        return subprocess.CompletedProcess(cmd, 0, "", "")


class _GuardFails(_Net):
    """A host that will not take the fail-closed guard."""

    def apply_guard(self, plan):
        return NetResult(ok=False, error="nft denied")


def _state(settings, stub_xray, net):
    settings.xray_bin = stub_xray
    state = build_state(settings, net=net)
    state.dnsmasq = state.pd_client = None
    return state


def _connected(state, stub_xray) -> Node:
    """The gateway the operator has: a node applied, xray up, the tproxy ruleset on the host."""
    node = state.store.get_node(state.store.add_node(Node(
        id=None, name="n1", address="1.2.3.4", port=47000, uuid="u-1",
        sni="www.microsoft.com", public_key="PK", short_id="ab12")))
    assert apply_node(node, state.settings, state.supervisor, state.net,
                      store=state.store, xray_bin=stub_xray).ok is True
    assert state.supervisor.status()["running"] is True
    assert "tproxy" in state.net.applied[-1]
    return node


def _document(state, **net) -> dict:
    """A backup as it reaches the panel from the operator: the JSON round-trip a download-then-upload
    is, with no memory of having been exported here — which is how the panel must treat anything
    handed to a restore, and what makes the lockout guard apply."""
    document = dict(backup_mod.export_state(state.store))
    document["settings"] = dict(document["settings"]) | net
    return document


def _unwritable(state, monkeypatch, key=UNDO_KEY):
    """A store that will not take ONE write — the transient failure the finding is about. Everything
    else behaves: a locked database, not a broken box."""
    original = state.store.set_setting

    def writing(name, value):
        if name == key:
            raise RuntimeError("database is locked")
        original(name, value)

    monkeypatch.setattr(state.store, "set_setting", writing)


def _cover_unreadable(state, monkeypatch) -> dict:
    """One of the enforcement cover's records stops answering, switchably. Everything else behaves —
    a locked database at render time, not a broken box. Returns the switch, so a test can decide the
    exact moment the render can no longer complete the cover."""
    switch = {"on": False}
    original = state.store.get_setting

    def reading(name):
        if switch["on"] and name == provision.SURVIVOR_KEY:
            raise RuntimeError("simulated settings read failure")
        return original(name)

    monkeypatch.setattr(state.store, "get_setting", reading)
    return switch


def _write_noop(state, monkeypatch, key=UNDO_KEY) -> dict:
    """A store that ACCEPTS a write to one key and keeps nothing, switchably. This is the silent
    no-op no clear can detect without reading back, and the whole reason
    `clear_provision_candidate` returns a result at all. Returns the switch, so a test can let the
    record be written and only then start dropping writes to it."""
    switch = {"on": False}
    original = state.store.set_setting

    def writing(name, value):
        if switch["on"] and name == key:
            return                      # accepted, dropped: the record keeps what it had
        original(name, value)

    monkeypatch.setattr(state.store, "set_setting", writing)
    return switch


def _trace(state, monkeypatch) -> list[str]:
    """The order the restore does the three things this is about in: writing the UNARMED candidate
    record, taking the runtime down, and ARMING the record once a pass may run. All call through — the
    sequence is what is under test, not the effect."""
    events: list[str] = []
    record, stop = provision.record_provision_candidate, state.supervisor.stop

    def recording(store, candidate):
        kind = "record" if candidate.get("armed") is False else "arm"
        events.append(f"{kind}:{candidate.get('iface', '')}")
        return record(store, candidate)

    def stopping():
        events.append("stop")
        return stop()

    monkeypatch.setattr(provision, "record_provision_candidate", recording)
    monkeypatch.setattr(state.supervisor, "stop", stopping)
    return events


def _settled(state):
    """The candidate record is in a state no later pass acts on: it names nothing for the enforcement
    to cover, the cover can read it, and the next boot's resume finds no work."""
    assert state.store.get_setting(UNDO_KEY) in ("", provision.RESOLVED_UNDO)
    assert provision.pending_candidate_ifaces(state.store) == []
    assert provision.enforcement_cover(state.store, "eth0.2").known is True
    outcome = provision.resume_pending_provision_undo(state)
    assert outcome.actions == [] and outcome.unresolved == [], \
        "a restore that never provisioned anything left an undo for the next boot"


def test_a_transient_store_failure_leaves_the_gateway_running(settings, stub_xray, monkeypatch):
    """PRODUCT-CRITICAL ORDERING. The candidate record cannot be proven, so the restore refuses —
    and refusing now costs nothing: xray is still serving the node it was serving, the disconnected
    guard was never installed, the tproxy ruleset is still the one on the host, and the store still
    names the active node. Before the reorder this returned the same error with the gateway down."""
    net = _Net()
    state = _state(settings, stub_xray, net)
    node = _connected(state, stub_xray)
    document = _document(state)
    applied = list(net.applied)
    events = _trace(state, monkeypatch)
    _unwritable(state, monkeypatch)

    try:
        result = restore_backup(state, document)

        assert result.ok is False
        assert "could not record what the restore is about to put on the host" in result.error
        assert events == ["record:eth0.2"], \
            f"the runtime was touched before the record was proven: {events}"
        assert state.supervisor.status()["running"] is True, \
            "a restore that imported nothing still disconnected the gateway"
        assert net.applied == applied, "the disconnected guard went on before the restore could run"
        assert state.store.get_setting("active_node_id") == str(node.id)
        assert state.store.list_nodes()[0].name == "n1"          # nothing was imported either
    finally:
        monkeypatch.undo()
        state.supervisor.stop()
        state.close()


def test_a_candidate_that_cannot_be_rendered_refuses_before_the_disconnect(settings, stub_xray,
                                                                          monkeypatch):
    """PRODUCT-CRITICAL. The render raises — hand-edited `segment_ip` reaching `ip_network()` is the
    real shape — and the failure used to be spent as `candidate = {}`, which is this path's value for
    "the pass will put nothing anywhere". The restore then disconnected and provisioned with NO undo
    record, so the candidate interface a rollback or a crash left behind was named nowhere: outside
    the restored enforcement cover and invisible to every later pass. Refusing here costs nothing."""
    net = _Net()
    state = _state(settings, stub_xray, net)
    node = _connected(state, stub_xray)
    document = _document(state)
    applied = list(net.applied)
    events = _trace(state, monkeypatch)

    def raising(*_a, **_kw):
        raise ValueError("'not-an-ip' does not appear to be an IPv4 or IPv6 network")

    monkeypatch.setattr(provision, "provision_candidate", raising)

    try:
        result = restore_backup(state, document)

        assert result.ok is False
        assert "could not establish what the restore would put on the host" in result.error
        assert events == [], f"the runtime was touched after the candidate render failed: {events}"
        assert state.supervisor.status()["running"] is True, \
            "a restore with no undo record still disconnected the gateway"
        assert net.applied == applied, "the disconnected guard went on with no candidate recorded"
        assert not state.store.get_setting(UNDO_KEY)
        assert state.store.get_setting("active_node_id") == str(node.id)
        assert state.store.list_nodes()[0].name == "n1"          # nothing was imported either
    finally:
        monkeypatch.undo()
        state.supervisor.stop()
        state.close()


def test_the_stop_that_does_not_happen_leaves_no_candidate_behind(settings, stub_xray, monkeypatch):
    """The first exit AFTER the record is written. No pass runs, so the record must not outlive the
    request: left set it names an interface every later ruleset covers, and hands
    `resume_pending_provision_undo` host state this restore never created."""
    net = _Net()
    state = _state(settings, stub_xray, net)
    _connected(state, stub_xray)
    document = _document(state)
    applied = list(net.applied)
    events = _trace(state, monkeypatch)
    monkeypatch.setattr(state.supervisor, "status", lambda: {"running": True})   # the stop did not take

    try:
        result = restore_backup(state, document)

        assert result.ok is False and result.error == "xray did not stop before restore"
        assert result.snapshot, "the recovery copy this restore took was not named in its answer"
        assert events == ["record:eth0.2", "stop"]           # proven first, then the disconnect
        assert net.applied == applied                        # nothing was enforced over a live xray
        _settled(state)
    finally:
        monkeypatch.undo()
        state.supervisor.stop()
        state.close()


def test_an_unreadable_cover_refuses_before_the_disconnect(settings, stub_xray, monkeypatch):
    """PRODUCT-CRITICAL. The disconnected guard is a store-derived render, so an unreadable cover
    source means there is NO plan to install. Asked where the guard was installed — after
    `supervisor.stop()` — that answer was a gateway outage: xray down, the previous apply's tproxy
    ruleset still on the host pointing at it, nothing imported. Rendered before the stop it is a
    refusal that costs nothing."""
    net = _Net()
    state = _state(settings, stub_xray, net)
    node = _connected(state, stub_xray)
    document = _document(state)
    applied = list(net.applied)
    events = _trace(state, monkeypatch)
    unreadable = _cover_unreadable(state, monkeypatch)
    unreadable["on"] = True

    try:
        result = restore_backup(state, document)

        assert result.ok is False
        assert "could not enforce disconnected state" in result.error
        assert "could not be established" in result.error
        assert events == ["record:eth0.2"], \
            f"the runtime was taken down for a guard that could not be rendered: {events}"
        assert state.supervisor.status()["running"] is True, \
            "an unrenderable guard disconnected the gateway"
        assert net.applied == applied            # the ruleset the operator had is still the one on
        assert state.store.get_setting("active_node_id") == str(node.id)
        assert state.store.list_nodes()[0].name == "n1"
        unreadable["on"] = False
        _settled(state)
    finally:
        monkeypatch.undo()
        state.supervisor.stop()
        state.close()


def test_the_guard_installed_after_the_disconnect_is_the_pre_rendered_one(settings, stub_xray,
                                                                         monkeypatch):
    """The other half: the plan applied after the stop is the one rendered BEFORE it. A rolled-back
    change left `eth0.9` up carrying the segment address, so the complete cover names it — and the
    cover becomes unreadable the moment xray goes down. A restore that re-derived the plan there
    would refuse at exactly the point the pre-render exists to move away from; the retained plan
    still covers both interfaces."""
    net = _Net()
    state = _state(settings, stub_xray, net)
    _connected(state, stub_xray)
    assert provision.remember_survivors(state.store, [("eth0.9", "192.168.10.2/24")]) is True
    document = _document(state)
    unreadable = _cover_unreadable(state, monkeypatch)
    guards: list = []

    stop = state.supervisor.stop
    original_guard = net.apply_guard

    def stopping():
        outcome = stop()
        unreadable["on"] = True          # from here no render can complete the cover
        return outcome

    def guarding(plan):
        guards.append(plan)
        unreadable["on"] = False         # the retained plan has reached the backend
        return original_guard(plan)

    monkeypatch.setattr(state.supervisor, "stop", stopping)
    monkeypatch.setattr(net, "apply_guard", guarding)

    try:
        result = restore_backup(state, document)

        assert result.ok is True, result.error
        assert guards, "no guard reached the host: the plan was re-derived after the disconnect"
        assert guards[0].segment_iface == "eth0.2"
        assert guards[0].extra_ifaces == ("eth0.9",), \
            "the guard applied after the disconnect was not the pre-rendered one"
    finally:
        monkeypatch.undo()
        state.supervisor.stop()
        state.close()


def test_a_guard_that_cannot_be_installed_leaves_no_candidate_behind(settings, stub_xray):
    """The second exit after the record is written: the host refuses the disconnected guard. The
    restore stops there — nothing is imported, no host pass runs — so the record is settled for the
    same reason. Being disconnected IS the answer on this one: xray is down and the ruleset the
    previous apply installed still points at it, so clients are black-holed, not released."""
    net = _GuardFails()
    state = _state(settings, stub_xray, net)
    _connected(state, stub_xray)
    document = _document(state)

    try:
        result = restore_backup(state, document)

        assert result.ok is False
        assert "could not enforce disconnected state" in result.error
        assert state.supervisor.status()["running"] is False
        assert state.store.list_nodes()[0].name == "n1"           # nothing imported
        _settled(state)
    finally:
        state.supervisor.stop()
        state.close()


def test_a_successful_restore_still_disconnects_imports_and_enforces(settings, stub_xray,
                                                                    monkeypatch):
    """The success path, unchanged: the runtime IS taken down, the document is imported, the active
    selection is cleared, the fail-closed guard is what ends up on the host, and the candidate record
    is still settled by the commit. The record is now written unarmed before the disconnect and armed
    after it, once a host pass may actually run."""
    net = _Net()
    state = _state(settings, stub_xray, net)
    _connected(state, stub_xray)
    document = _document(state)
    events = _trace(state, monkeypatch)

    try:
        result = restore_backup(state, document)

        assert result.ok is True and result.summary and result.snapshot
        assert events == ["record:eth0.2", "stop", "arm:eth0.2"]
        assert state.supervisor.status()["running"] is False
        assert state.store.get_setting("active_node_id") == ""
        assert state.store.get_setting("active_since") == ""
        guard = net.applied[-1]
        assert "tproxy" not in guard and "drop" in guard
        assert set(re.findall(r'iifname "([^"]+)"', guard)) == {"eth0.2"}
        _settled(state)
    finally:
        monkeypatch.undo()
        state.supervisor.stop()
        state.close()


# --- the record that describes an intention -----------------------------------------------------


def _stranded(state, monkeypatch, net, exit_at: str):
    """Drive a restore to one of the two post-disconnect exits with the candidate record's own key
    silently dropping every write from the moment the record lands. So the settle is a no-op, the
    record survives the request, and what it says is all that stands between the next boot and an
    `ip link delete`.

    The document retargets the segment to `eth0.9`, which is NOT on the host: that is the one shape
    whose ARMED record licenses a delete (a VLAN, proven absent before the change). Nothing here
    creates it — the restore never gets that far — and the caller puts it on the host afterwards, the
    way an operator, netplan or NetworkManager would.
    """
    document = _document(state, segment_iface="eth0.9")
    dropped = _write_noop(state, monkeypatch)
    record = provision.record_provision_candidate

    def recording(store, candidate):
        record(store, candidate)
        dropped["on"] = True             # from here nothing can change the record
    monkeypatch.setattr(provision, "record_provision_candidate", recording)
    if exit_at == "stop":
        monkeypatch.setattr(state.supervisor, "status", lambda: {"running": True})

    result = restore_backup(state, document)

    assert result.ok is False
    dropped["on"] = False                # the transient fault passes; the record does not
    assert state.store.get_setting(UNDO_KEY), "the record under test was not left behind"
    return result


@pytest.mark.parametrize("exit_at", ["stop", "guard"])
def test_a_settle_that_no_ops_leaves_boot_no_licence_to_undo(settings, stub_xray, monkeypatch,
                                                            exit_at):
    """PRODUCT-CRITICAL. Both post-disconnect exits settle the record best-effort and used to drop the
    result, so a store that accepted the write and kept nothing left the record behind unnoticed. The
    record is written UNARMED for exactly that: the restore ran no host command, so the next boot may
    not delete the candidate link — even though by then the interface exists, put there by something
    else entirely."""
    net = _Net() if exit_at == "stop" else _GuardFails()
    state = _state(settings, stub_xray, net)
    _connected(state, stub_xray)

    try:
        _stranded(state, monkeypatch, net, exit_at)
        held = json.loads(state.store.get_setting(UNDO_KEY))
        assert held["iface"] == "eth0.9" and held["armed"] is False
        net.links.add("eth0.9")                  # the host has it now; the restore did not put it there
        del net.cmds[:]

        outcome = provision.resume_pending_provision_undo(state)

        assert [cmd for cmd in net.cmds if cmd[1:3] == ["link", "delete"]] == [], \
            "boot deleted a candidate link this restore never created"
        assert "eth0.9" in net.links
        assert outcome.actions == [] and outcome.unresolved == []
        assert provision._parse_survivors(state.store) == [], \
            "an intention was recorded as host state a rolled-back change left behind"
        assert provision.pending_candidate_ifaces(state.store) == []      # settled by the resume
    finally:
        monkeypatch.undo()
        state.supervisor.stop()
        state.close()


def test_an_unarmed_record_is_covered_but_not_deletable(settings, stub_xray, monkeypatch):
    """The two halves of "unarmed", together, because they pull in opposite directions. The record is
    deliberately a COVERAGE source, so its interface must stay in every rendered ruleset — naming an
    interface costs nothing and dropping one is the direct-WAN bypass. It is also a licence to remove
    host state, and there it must count for nothing until a pass has actually run."""
    net = _Net()
    state = _state(settings, stub_xray, net)
    _connected(state, stub_xray)

    try:
        _stranded(state, monkeypatch, net, "stop")

        # COVERED: the record names the interface, the cover is complete, and the rendered guard
        # scopes its rules to it as well as to the configured segment.
        assert provision.pending_candidate_ifaces(state.store) == ["eth0.9"]
        cover = provision.enforcement_cover(state.store, "eth0.2")
        assert cover.names == ["eth0.9"] and cover.known is True
        plan = controller._enforcement_plan(state.settings, state.store)
        assert plan.segment_iface == "eth0.2" and plan.extra_ifaces == ("eth0.9",)

        # NOT DELETABLE: the same record, read by the undo, licenses nothing at all.
        net.links.add("eth0.9")
        del net.cmds[:]
        outcome = provision.undo_provision_candidate(
            state, json.loads(state.store.get_setting(UNDO_KEY)))
        assert [cmd for cmd in net.cmds if cmd[1:3] == ["link", "delete"]] == []
        assert outcome.actions == [] and outcome.unresolved == []
    finally:
        monkeypatch.undo()
        state.supervisor.stop()
        state.close()


def test_the_record_is_armed_before_the_host_pass_runs(settings, stub_xray, monkeypatch):
    """The other side of the same line, and the reason "unarmed" is a phase and not a policy. Once
    `host_provision` may create the candidate link, the record on disk has to be the FULL one: a crash
    between the pass and the commit leaves nothing else that can reclaim the interface, and the undo
    needs the VLAN fact and the prior probe before it is allowed to."""
    net = _Net()
    state = _state(settings, stub_xray, net)
    _connected(state, stub_xray)
    document = _document(state, segment_iface="eth0.9")
    seen: list[dict] = []
    host_provision = provision.host_provision

    def provisioning(st):
        seen.append(json.loads(st.store.get_setting(UNDO_KEY) or "{}"))
        return host_provision(st)

    monkeypatch.setattr(provision, "host_provision", provisioning)

    try:
        result = restore_backup(state, document)

        assert result.ok is True, result.error
        assert seen, "the host pass never ran"
        armed = seen[0]
        assert armed["iface"] == "eth0.9"
        assert armed["vlan"] is True                    # the VLAN fact the undo needs...
        assert armed["link_state"] == "absent"          # ...and the probe that licenses the delete
        assert armed["addr4"], "the addresses the undo reports as orphans were not recorded"
        assert "armed" not in armed, \
            "an armed record is the one PUT /api/network writes; only `armed: False` marks a phase"
    finally:
        monkeypatch.undo()
        state.supervisor.stop()
        state.close()


@pytest.mark.parametrize("phase", ["record", "stop"])
def test_the_lockout_guard_still_runs_before_anything(settings, stub_xray, monkeypatch, phase):
    """The non-negotiable this reorder must not disturb: a document that would move the segment onto
    the management leg is refused by `validate_document` FIRST — before the snapshot, before the
    candidate record, before the runtime is touched. Neither of the two things now ahead of the
    disconnect may run for it."""
    net = _Net()
    state = _state(settings, stub_xray, net)
    _connected(state, stub_xray)
    document = _document(state, segment_iface=state.settings.mgmt_iface)
    if phase == "record":
        monkeypatch.setattr(provision, "record_provision_candidate",
                            lambda *_a, **_kw: pytest.fail("a rejected document reached the record"))
    else:
        monkeypatch.setattr(state.supervisor, "stop",
                            lambda: pytest.fail("a rejected document stopped xray"))

    try:
        with pytest.raises(ValueError, match="mgmt_iface"):
            restore_backup(state, document)
        assert state.supervisor.status()["running"] is True
        assert not state.store.get_setting(UNDO_KEY)       # never written at all
    finally:
        monkeypatch.undo()
        state.supervisor.stop()
        state.close()
