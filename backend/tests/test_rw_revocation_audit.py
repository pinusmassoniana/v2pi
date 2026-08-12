"""Revocation defects found by audit: the ways a REVOKED remote-access credential stays usable.

Every test here is written so it fails if one specific defect comes back. They share one shape —
arm the road-warrior inbound with two clients, revoke one, and then ask whether the credential is
really gone from the store, from the config on disk, and from the running process.

The base fixtures and helpers live in `test_rw_inbound.py`; this module imports them rather than
growing a second copy that can drift from the first.
"""
import json
import sqlite3
import threading
import time

import pytest
from conftest import _client, _login
from test_rw_inbound import _armed_with_two_clients, _live_client_ids


def _unqueryable_status():
    """A supervisor whose state cannot be observed — the `running is None` reading."""
    raise OSError("cannot query the supervisor")


def _rw_events(c) -> list[str]:
    from pi_gw_panel.net_control import events as conn_events
    return [e["detail"] for e in conn_events.recent(c.app.state.app_state.store)
            if e["kind"] == "rw-revoke"]


def _client_ids(c) -> list[str]:
    return [x["id"] for x in c.get("/api/rw").json()["clients"]]


# --- F1: nothing after the credential is removed may raise -------------------------------
#
# The client is deleted from the store BEFORE the runtime work starts, so anything that goes
# wrong afterwards must not be able to take the deletion with it. Guarding the calls one by one
# was only half of it — the runtime work also ran inside the handler's DB transaction, and a
# NESTED transaction failing there discards the whole unit no matter how well guarded the call
# that contains it is (see F1b). The revocation now commits first and does its runtime work
# afterwards, still under `apply_lock`, so these tests pin the guards and the ordering together.


def test_a_supervisor_that_throws_on_stop_does_not_undo_the_revocation(settings, stub_xray):
    """`supervisor.stop()` shells out to terminate/wait/kill. It was unguarded, so an OSError
    from any of them travelled out of the revocation and rolled the client deletion back with
    it — leaving the operator an error, a device still granted in the store, and an xray in
    whatever state the failed stop had reached. A stop that fails is a fact to record."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    _nid, lost, kept = _armed_with_two_clients(c, h)

    sup = c.app.state.app_state.supervisor
    real_status, real_stop = sup.status, sup.stop
    sup.status = _unqueryable_status          # unknown state → the fail-safe stop branch
    sup.stop = lambda: (_ for _ in ()).throw(OSError("terminate: no such process"))
    try:
        r = c.delete(f"/api/rw/clients/{lost}", headers=h)
    finally:
        sup.status, sup.stop = real_status, real_stop

    assert r.status_code == 200, "a failed stop turned a completed revocation into a 500"
    # `stop-failed`, not `stopped`: the stop raised, so nothing here observed xray going down and
    # the answer may not claim it did (see F6).
    assert r.json()["revocation"] == "stop-failed"
    assert lost not in _client_ids(c), \
        "the client deletion was rolled back by a failure that happened after it"
    assert kept in _client_ids(c)
    details = _rw_events(c)
    assert any("tried and failed to stop xray" in d for d in details), \
        "the incident record claims a stop that never happened, or lost the event entirely"
    assert lost not in _live_client_ids(settings), "the revoked uuid is still in the config"


def test_a_network_stop_that_cannot_render_its_plan_does_not_undo_the_revocation(settings,
                                                                                stub_xray,
                                                                                monkeypatch):
    """`stop_net` evaluated `NetPlan.from_store(...)` as an ARGUMENT, i.e. outside the try/except
    in `_call_net` — so it could raise even though every caller treats it as a function that
    reports failure by returning a NetResult. Malformed stored net settings (hand-edited DB,
    foreign backup) are exactly what makes the render raise, and they must not be able to veto a
    revocation that has already taken the credential out of the store."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    _nid, lost, kept = _armed_with_two_clients(c, h)

    class _Unrenderable:
        """Stands in for NetPlan inside the controller only — patching the real classmethod
        would also break the plain `routed_nets` read the response body does."""
        @classmethod
        def from_store(cls, store, s):
            raise ValueError("segment_ip is not an address")

    sup = c.app.state.app_state.supervisor
    real_status = sup.status
    sup.status = _unqueryable_status          # unknown state → the fail-safe stop branch
    monkeypatch.setattr("pi_gw_panel.controller.NetPlan", _Unrenderable)
    try:
        r = c.delete(f"/api/rw/clients/{lost}", headers=h)
    finally:
        sup.status = real_status
        monkeypatch.undo()

    assert r.status_code == 200, "an unrenderable net plan turned a revocation into a 500"
    assert r.json()["revocation"] == "stopped"
    assert lost not in _client_ids(c), \
        "the client deletion was rolled back by a network failure after it"
    assert kept in _client_ids(c)
    assert any("network stop failed" in d for d in _rw_events(c)), \
        "the network failure was neither reported nor recorded"
    assert lost not in _live_client_ids(settings)


def test_a_write_whose_setup_raises_still_sanitizes_the_config(settings, stub_xray, monkeypatch):
    """`_rw_write_config` promises in its own docstring to fail safe rather than raise, but its
    SETUP — the ConfigManager and the node lookup — sat outside the per-producer try. A raise
    there left the function through the one exit no caller handles.

    The node lookup failing is not even fatal: sanitizing the config on disk needs no node, so
    the revocation must still complete rather than take the deletion down with it."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)      # the write path, not the reapply

    def _boom(state):
        raise TypeError("the stored node id is not an int")

    monkeypatch.setattr("pi_gw_panel.api.routes._rw_rebuild_node", _boom)
    r = c.delete(f"/api/rw/clients/{lost}", headers=h)
    monkeypatch.undo()

    assert r.status_code == 200, "a raising node lookup turned a revocation into a 500"
    assert r.json()["revocation"] == "rebuilt"
    assert lost not in _client_ids(c), \
        "the client deletion was rolled back by a failure in the config write's setup"
    assert _live_client_ids(settings) == [kept], "the revoked uuid survived in the live config"


# --- F1b: a NESTED transaction is a rollback the guards cannot catch ----------------------


def _fail_one_setting(store, key: str):
    """Make one settings write fail the way a full disk or a locked database does, and record
    that it fired. Returns (restore, hits)."""
    real_set = store.set_setting
    hits: list[str] = []

    def _failing(name, value):
        if name == key:
            hits.append(name)
            raise sqlite3.OperationalError("disk I/O error")
        return real_set(name, value)

    store.set_setting = _failing
    return (lambda: setattr(store, "set_setting", real_set)), hits


def test_a_failed_persist_inside_the_rebuild_does_not_undo_the_revocation(settings, stub_xray):
    """The revocation's own runtime work opens a transaction of its own.

    `apply_node` persists the active-node bookkeeping inside `with store.transaction()` after the
    reload and the net apply have already succeeded. Run from a handler that was itself one
    transaction, that block is NESTED: `_SafeConn` tracks depth, so a failure there marks the
    WHOLE unit rollback-only, and the outer exit then discards the client deletion and the
    incident event and raises — while every call around it was dutifully guarded and the
    revocation reported success. Guarding calls cannot fix this; only ordering can.

    Driven by failing the last write of that block — the traffic baseline — which is exactly the
    shape a full disk or a locked database takes. The clean config is on disk by then and xray is
    already serving it, so the deletion MUST survive: this asserts the store, not the absence of
    an exception.
    """
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    _nid, lost, kept = _armed_with_two_clients(c, h)      # node left ACTIVE → the rebuild path

    store = c.app.state.app_state.store
    restore, hits = _fail_one_setting(store, "session_base_up")
    try:
        r = c.delete(f"/api/rw/clients/{lost}", headers=h)
    finally:
        restore()

    assert hits, "the failure never reached the rebuild's persistence block — test is inert"
    assert r.status_code == 200, "a failed persist turned a completed revocation into a 500"
    assert lost not in _client_ids(c), \
        "the client deletion was rolled back by a nested transaction that failed after it"
    assert kept in _client_ids(c)
    assert lost not in _live_client_ids(settings), "the revoked uuid is still in the config"
    assert kept in _live_client_ids(settings)
    # The same rollback discarded the incident record — the entry an operator reads afterwards
    # to work out what the box did with a lost device.
    assert any("rebuilding the active node's config failed" in d for d in _rw_events(c)), \
        "the incident record went down with the rolled-back transaction"


# --- F2: "never start a stopped xray" has to be one operation, not two --------------------


def test_an_xray_that_dies_right_after_the_status_read_is_not_started_by_the_revocation(
        settings, stub_xray):
    """The gap between sampling the supervisor and acting on the sample.

    `_rw_revoke_apply` reads status() once at the top and releases the supervisor's lock, then
    reloads much later. `reload()` is an unconditional stop→start, so a child that exits inside
    that window is not reloaded but STARTED — on whatever config is on disk, serving whoever it
    names. The check and the restart have to happen under one hold of the supervisor's own lock,
    which is what `reload_if_running()` is for.

    Driven by killing the child from inside status(), the moment after it has answered
    "running": the tightest possible version of the race, and one no external locking can close.
    """
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)   # no active node: the write+reload path

    sup = c.app.state.app_state.supervisor
    real_status = sup.status
    sampled: list[bool] = []

    def _status_then_die():
        answer = real_status()
        if not sampled:
            sampled.append(bool(answer.get("running")))
            proc = sup._proc          # kill the child, leaving the supervisor none the wiser
            if proc is not None:
                proc.terminate()
                proc.wait(timeout=5)
        return answer

    sup.status = _status_then_die
    try:
        r = c.delete(f"/api/rw/clients/{lost}", headers=h)
    finally:
        sup.status = real_status

    assert sampled == [True], "the race was never set up — the sample did not say running"
    assert r.status_code == 200
    assert c.get("/api/status").json()["running"] is False, \
        "the revocation started an xray that had stopped between the check and the reload"
    assert lost not in _live_client_ids(settings), \
        "the revoked uuid is still in the config the next start would come up on"
    assert kept in _live_client_ids(settings)
    assert lost not in _client_ids(c)


def test_reload_if_running_reports_which_branch_it_took(settings, stub_xray):
    """The caller has to tell "reloaded" from "was not running" — collapsing them to a bool is
    how a not-running supervisor gets treated as a failed reload (or worse, a successful one)."""
    from pi_gw_panel.xray_supervisor.supervisor import XraySupervisor

    sup = XraySupervisor(stub_xray, str(settings.config_path))
    assert sup.reload_if_running() is None, "a stopped supervisor reported a reload"
    assert sup.status()["running"] is False, "reload_if_running started a stopped xray"

    sup.start()
    assert sup.status()["running"] is True
    try:
        assert sup.reload_if_running() is True, "a running supervisor was not reloaded"
        assert sup.status()["running"] is True
    finally:
        sup.stop()


# --- F3: a rollback may never reinstate a revoked credential ------------------------------


def _stale_pre_revocation_target(settings, xray_bin):
    """Leave the last-good snapshot holding the road-warrior inbound while the LIVE config does
    not, with the provenance pairing valid — i.e. a promotable config that predates a revocation.

    Built through ConfigManager's own writer rather than by hand, because the pairing is content-
    digest based: a file edited around it is not promotable and would prove nothing.
    """
    from pi_gw_panel.xray_config.validate import ConfigManager

    mgr = ConfigManager(settings, xray_bin=xray_bin)
    with open(settings.config_path) as f:
        with_rw = json.load(f)
    assert any(i.get("tag") == "rw-in" for i in with_rw["inbounds"])
    without_rw = json.loads(json.dumps(with_rw))
    without_rw["inbounds"] = [i for i in without_rw["inbounds"] if i.get("tag") != "rw-in"]
    without_rw["routing"]["rules"] = [r for r in without_rw["routing"]["rules"]
                                      if "rw-in" not in (r.get("inboundTag") or [])]
    assert mgr.apply(with_rw)[0]        # lastgood ← the live config, live ← the same content
    assert mgr.apply(without_rw)[0]     # lastgood ← the rw-bearing config, pairing valid
    assert mgr.rollback_available(), "the test did not actually create a promotable target"


def test_a_rollback_cannot_reinstate_a_credential_a_no_write_revocation_took_away(settings,
                                                                                 stub_xray,
                                                                                 monkeypatch):
    """The leak the sweep was the only guard against, on the path where it is still the only one.

    A revocation whose live config carries no `rw-in` writes NOTHING — there is nothing to cut —
    so its irreversible writer never runs and the pre-revocation pairing on disk is untouched.
    Dropping that pairing afterwards is the entire defence, and its failure was only logged while
    the revocation still answered success. A valid pairing then survives, and `/rollback` writes
    the snapshot back and reloads xray onto it: the revoked device is live again, one button
    later, with the panel having said it was cut off.

    Every rollback now goes through the same guard `/xray/start` uses, BEFORE anything is
    started or reloaded, so the restored file is reconciled with the store or refused.
    """
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    # Disconnect: no active node to reapply (so the revocation cannot take a WRITING path) but a
    # previous one on file, which is the branch where a rollback RELOADS xray onto the restored
    # config rather than merely stopping it.
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    _stale_pre_revocation_target(settings, stub_xray)

    with open(settings.config_path) as f:
        before = json.load(f)
    monkeypatch.setattr("pi_gw_panel.api.routes.ConfigManager.invalidate_rollback",
                        lambda self: False)                 # the sweep reports it could not
    c.delete(f"/api/rw/clients/{lost}", headers=h)
    monkeypatch.undo()

    # The premise, asserted on the file rather than on the outcome string: this revocation wrote
    # NO config, so its irreversible writer never ran and the pre-revocation pairing on disk is
    # untouched — which is what makes the sweep the only guard here. (The outcome is `rebuilt`,
    # not `not-live`: the config was already clean and the RUNNING process was reloaded onto it,
    # because a clean file is no evidence about what a live xray loaded minutes ago.)
    with open(settings.config_path) as f:
        assert json.load(f) == before, "the revocation wrote a config — this is now another test"
    assert lost not in _client_ids(c)
    assert c.get("/api/status").json()["rollback_available"] is True, \
        "the stale pre-revocation target was already gone — the leak was never set up"

    r = c.post("/api/rollback", headers=h)

    assert r.status_code in (200, 409), r.text
    assert lost not in _live_client_ids(settings), \
        "a rollback reinstalled the revoked credential the revocation had taken away"
    assert nid and kept


def test_a_rollback_that_cannot_be_sanitized_never_installs_the_candidate(settings, stub_xray,
                                                                         monkeypatch):
    """The guard ran, and it ran too late.

    The candidate was promoted to live FIRST and reconciled afterwards, so a sanitize that could
    not land (a full disk, an `xray -test` that rejects the result) answered 409 with the
    credential-bearing file already installed as the live config. Nothing put it back, and the
    file is what the next start comes up on — the watchdog's included, which asks no route for
    permission. A refusal has to mean the config was never installed.

    So the candidate is reconciled in hand and only a version the store actually grants is ever
    written. Here nothing validates, so nothing may be written at all: the live config must come
    out of the request byte-for-byte as it went in.
    """
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    _stale_pre_revocation_target(settings, stub_xray)

    with open(settings.config_path) as f:
        untouched = json.load(f)
    monkeypatch.setattr("pi_gw_panel.api.routes.ConfigManager.invalidate_rollback",
                        lambda self: False)             # the sweep reports it could not
    c.delete(f"/api/rw/clients/{lost}", headers=h)
    monkeypatch.undo()
    with open(settings.config_path) as f:
        before = json.load(f)
    assert before == untouched, "the revocation wrote a config — the pairing under test is stale"
    assert c.get("/api/status").json()["rollback_available"] is True, \
        "the promotable pre-revocation target was already gone — the leak was never set up"

    monkeypatch.setenv("STUB_XRAY_FAIL", "1")           # nothing the guard writes can validate
    r = c.post("/api/rollback", headers=h)
    monkeypatch.undo()

    assert r.status_code == 409, r.text
    assert lost not in _live_client_ids(settings), \
        "the refused rollback left the credential-bearing config installed as the live one"
    with open(settings.config_path) as f:
        assert json.load(f) == before, "a refused rollback still rewrote the live config"
    assert kept


def test_a_crashed_xray_is_not_restarted_onto_a_config_the_revocation_could_not_clean(
        settings, stub_xray, monkeypatch):
    """The watchdog is the caller no route-level guard can cover.

    A revocation that cannot write a clean config ends at the stop — and the stop is exactly what
    the next start undoes. `/xray/start` and `/rollback` check the file first, but the liveness
    loop restarts a crashed xray with a plain `supervisor.start()` on whatever is on disk, with
    no request and no handler in sight. Twenty seconds after the panel said the device was cut
    off, it was being served again by a process nobody asked to run.

    The guard therefore lives on the spawn itself. Refusing costs the operator a tunnel until the
    trouble clears; the second half of this test is that it DOES clear — the same start then
    sanitizes the file and comes up on it, so the fail-closed answer is not a dead end.
    """
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)    # xray stays up; nothing to rebuild from

    monkeypatch.setenv("STUB_XRAY_FAIL", "1")            # no config the revocation writes validates
    assert c.delete(f"/api/rw/clients/{lost}", headers=h).json()["revocation"] == "stopped"
    assert lost in _live_client_ids(settings), "the write was supposed to be impossible here"
    assert c.get("/api/status").json()["running"] is False

    sup = c.app.state.app_state.supervisor
    sup.start()                                          # the watchdog's restart, verbatim
    assert sup.status()["running"] is False, \
        "a bare restart served a config still granting the revoked credential"

    monkeypatch.undo()                                   # whatever blocked the write clears
    sup.start()
    try:
        assert sup.status()["running"] is True, "the guard stayed shut once it could clean the file"
        assert _live_client_ids(settings) == [kept], \
            "the restart came up on the revoked credential instead of cleaning it first"
    finally:
        sup.stop()


# --- F4: "nothing was cut" and "the config was rewritten" are different outcomes -----------


def test_the_two_no_reload_outcomes_do_not_share_one_value(settings, stub_xray):
    """Two branches, structurally different, that both used to answer `not-live`.

      * The config provably carried no `rw-in`: nothing was serving it, nothing was written.
      * The config DID carry it, was rewritten to strip the credential, and xray is
        affirmatively down: a completed, durable revocation.

    One string cannot serve both, and the one it shared says the first — so the screen told
    operators "nothing was cut" about a revocation that had just been made permanent. The whole
    fix is that they are distinguishable, so this asserts both values from one client.
    """
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    c.post("/api/xray/stop", headers=h)                   # down, and the file still names `lost`
    assert lost in _live_client_ids(settings)

    body = c.delete(f"/api/rw/clients/{lost}", headers=h).json()
    assert body["revocation"] == "cleaned", \
        "a rewritten config was reported with the value that means nothing was written"
    assert lost not in _live_client_ids(settings)
    assert kept in _live_client_ids(settings)

    # ...and now the genuine no-op: the remaining client is deleted, so `resolve()` emits no
    # inbound at all and the next revocation finds a config with nothing in it to cut.
    assert c.delete(f"/api/rw/clients/{kept}", headers=h).json()["revocation"] == "cleaned"
    assert _live_client_ids(settings) == []
    c.post("/api/rw/clients", json={"email": "tablet"}, headers=h)
    fresh = c.get("/api/rw").json()["clients"][0]["id"]
    body = c.delete(f"/api/rw/clients/{fresh}", headers=h).json()
    assert body["revocation"] == "not-live", \
        "a revocation that wrote nothing borrowed the value for one that did"


# --- F5: the commit/runtime gap has to survive the panel dying in the middle of it --------
#
# A revocation commits the credential change and only THEN does its runtime work — it has to,
# because the runtime work opens transactions of its own and a failure in one used to discard the
# whole unit. That ordering is right and it opens a window, and the window is not covered by the
# guards that cover every other way a stale config reaches xray. The ones that exist all gate a
# SPAWN: `_rw_guard_start` on `/xray/start`, `_rw_guard_promotion` on a rollback, and the
# supervisor's own start guard on the watchdog's restart. An xray that is already running needs
# no spawn. It loaded the pre-revocation config minutes ago, it keeps serving it, and the
# watchdog leaves it alone because a process serving a revoked credential is perfectly healthy.
#
# So the gap is recorded durably, in the same transaction as the mutation that opens it, and
# cleared only once the runtime work reached an outcome that proves the credential is gone.
# Startup and every liveness tick finish whatever is still marked. Each test below interrupts one
# of the three narrowing routes IMMEDIATELY after its transaction exits and then demands the
# recovery finish the job against a HEALTHY, RUNNING xray.


class _Interrupted(Exception):
    """The panel dying between the commit and the runtime work — a thread killed, a container
    stopped, an OOM. Injected exactly where the transaction ends."""


def _interrupt_after_commit(monkeypatch):
    """Replace the runtime half of the revocation with a process death. Whatever the handler
    committed is on disk; nothing after it ran."""
    from pi_gw_panel.api import routes

    def _die(_state):
        raise _Interrupted("the panel died between the commit and the runtime work")

    monkeypatch.setattr(routes, "_rw_revoke", _die)


def _pending(c) -> bool:
    from pi_gw_panel.api.routes import RW_PENDING_KEY
    return bool(c.app.state.app_state.store.get_setting(RW_PENDING_KEY))


def _liveness_reconcile(c) -> None:
    """One liveness tick's worth of recovery, against the state as it stands."""
    from pi_gw_panel.health.liveness import LivenessLoop
    LivenessLoop(c.app.state.app_state)._reconcile_tick()


def _armed_and_interrupted(c, h, monkeypatch, do):
    """Arm two clients, run `do` (one narrowing request) with the runtime half killed, and
    assert the state the window is made of: committed store, marker set, xray still serving."""
    nid, lost, kept = _armed_with_two_clients(c, h)
    _interrupt_after_commit(monkeypatch)
    with pytest.raises(_Interrupted):
        do(lost)
    monkeypatch.undo()
    state = c.app.state.app_state
    assert state.supervisor.status()["running"] is True, \
        "the fixture must leave a RUNNING xray — the case no other guard covers"
    assert state.supervisor.state() == "working", \
        "the watchdog only acts on 'error'; a stale-but-healthy process is what it cannot see"
    assert _pending(c), \
        "the interrupted revocation left no durable trace, so nothing can ever finish it"
    return nid, lost, kept


def test_an_interrupted_delete_is_finished_by_the_liveness_loop(settings, stub_xray, monkeypatch):
    """DELETE /rw/clients/{id}, killed the instant its transaction closes.

    The deletion is committed — that is the fix this window is the price of — and the running
    xray is still serving the uuid off the config it loaded. Nothing spawns, so no start guard
    fires; the process is healthy, so the watchdog passes over it. Only the marker can tell
    anyone the runtime is behind, and only the liveness loop is looking.
    """
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    _nid, lost, kept = _armed_and_interrupted(
        c, h, monkeypatch, lambda cid: c.delete(f"/api/rw/clients/{cid}", headers=h))

    assert lost not in _client_ids(c), "the deletion did not commit before the interruption"
    assert lost in _live_client_ids(settings), \
        "the window this test is about did not open — nothing left to prove"

    _liveness_reconcile(c)

    assert _live_client_ids(settings) == [kept], \
        "the running xray was left serving a credential the store had already given up"
    assert not _pending(c), "the marker survived a completed reconciliation and will loop"


def test_an_interrupted_suspend_is_finished_by_the_liveness_loop(settings, stub_xray, monkeypatch):
    """PATCH /rw/clients/{id} {enabled: false} — same window, same recovery."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    _nid, lost, kept = _armed_and_interrupted(
        c, h, monkeypatch,
        lambda cid: c.patch(f"/api/rw/clients/{cid}", json={"enabled": False}, headers=h))

    suspended = next(x for x in c.get("/api/rw").json()["clients"] if x["id"] == lost)
    assert suspended["enabled"] is False, "the suspension did not commit before the interruption"
    assert lost in _live_client_ids(settings)

    _liveness_reconcile(c)

    assert _live_client_ids(settings) == [kept]
    assert not _pending(c)


def test_an_interrupted_narrowing_save_is_finished_at_startup(settings, stub_xray, monkeypatch):
    """PUT /rw that switches the feature off — and the OTHER consumer of the marker.

    The liveness loop covers a panel that stayed up. A panel that was restarted (the container
    stopped mid-revocation, which is the likelier shape of this) has to finish it before it
    serves anything, so the lifespan runs the same recovery. Driven here through the public
    entry point both consumers call.
    """
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    from test_rw_inbound import RW_ARMED
    _nid, lost, _kept = _armed_and_interrupted(
        c, h, monkeypatch,
        lambda _cid: c.put("/api/rw", json={**RW_ARMED, "enabled": False, "private_key": ""},
                           headers=h))

    assert c.get("/api/rw").json()["enabled"] is False, "the save did not commit"
    assert lost in _live_client_ids(settings), "the window did not open"

    from pi_gw_panel.api.routes import rw_reconcile_pending
    assert rw_reconcile_pending(c.app.state.app_state) is True

    assert _live_client_ids(settings) == [], \
        "the inbound the operator switched off was still live in the running config"
    assert not _pending(c)


def test_the_recovery_never_starts_an_xray_the_operator_stopped(settings, stub_xray, monkeypatch):
    """The failure mode a naive "pending → restart" recovery would have.

    A marker plus a stopped xray must not read as "put it back up". The revocation is an
    access-control action: it may take access away and may never hand any back, least of all by
    resurrecting a process the operator deliberately stopped. The reconcile still has to finish
    — the stored config must stop naming the revoked client — it just does it by rewriting the
    file, which is safe in every supervisor state.
    """
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    c.post("/api/xray/stop", headers=h)
    _interrupt_after_commit(monkeypatch)
    with pytest.raises(_Interrupted):
        c.delete(f"/api/rw/clients/{lost}", headers=h)
    monkeypatch.undo()
    assert _pending(c) and c.get("/api/status").json()["running"] is False

    _liveness_reconcile(c)

    assert c.get("/api/status").json()["running"] is False, \
        "the recovery started an xray the operator had stopped"
    assert _live_client_ids(settings) == [kept], \
        "the config the next start comes up on still names the revoked client"
    assert not _pending(c)


def test_a_marker_that_cannot_clear_backs_off_instead_of_wedging_the_panel(settings, stub_xray):
    """A stuck marker must be inert, not a treadmill.

    Three properties, because the recovery is the kind of thing that turns one broken box into a
    dead panel: it must not block a request, it must not respawn anything, and it must not file
    an event on every tick — the connection log is 40 entries deep and it is what the operator
    reads to work out what happened to the lost device.
    """
    from pi_gw_panel.api.routes import RW_PENDING_KEY
    from pi_gw_panel.health.liveness import DEFAULT_INTERVAL, LivenessLoop

    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    _armed_with_two_clients(c, h)
    state = c.app.state.app_state
    state.store.set_setting(RW_PENDING_KEY, "1")

    now = [1000.0]
    attempts: list[float] = []

    def _never_resolves(_st) -> bool:
        attempts.append(now[0])
        return False

    loop = LivenessLoop(state, now=lambda: now[0], reconcile=_never_resolves)
    for _ in range(40):
        loop._reconcile_tick()
        now[0] += DEFAULT_INTERVAL

    assert len(attempts) < 12, f"the stuck marker was retried {len(attempts)} times in 40 ticks"
    assert attempts[-1] - attempts[-2] >= 300.0, "the retry never backed off"
    assert sum("did not reach the running xray" in d for d in _rw_events(c)) == 1, \
        "every retry filed its own event — the 40-entry connection log flushes in minutes"
    # The panel is still answering, and the marker has refused nothing.
    assert c.get("/api/rw").status_code == 200
    assert c.post("/api/rw/clients", json={"email": "tablet"}, headers=h).status_code == 201


def test_a_new_revocation_does_not_inherit_the_previous_episode_s_backoff(settings, stub_xray):
    """Backing off is per EPISODE, and an episode ends when the marker goes.

    Observing that a marker has cleared reset the attempt counter and the reported flag and left
    the next-attempt DEADLINE where the last failed episode had pushed it — ten minutes out. A
    fresh revocation interrupted anywhere inside that window then inherited a deadline it had
    nothing to do with, and the credential it had already taken out of the store stayed live on
    the running xray for the remainder of it. Nothing else is looking: the process is healthy, so
    the watchdog passes over it, and no spawn means no start guard.

    Driven from the worst case — an episode driven all the way to the 600 s ceiling, then
    cleared, then a new marker one tick later, which must be acted on at once.
    """
    from pi_gw_panel.api.routes import RW_PENDING_KEY
    from pi_gw_panel.health.liveness import DEFAULT_INTERVAL, LivenessLoop

    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    _armed_with_two_clients(c, h)
    state = c.app.state.app_state

    now = [1000.0]
    attempts: list[float] = []

    def _never_resolves(_st) -> bool:
        attempts.append(now[0])
        return False

    loop = LivenessLoop(state, now=lambda: now[0], reconcile=_never_resolves)
    state.store.set_setting(RW_PENDING_KEY, "1")
    for _ in range(200):                       # long enough to reach the 600 s ceiling
        loop._reconcile_tick()
        now[0] += DEFAULT_INTERVAL
    assert attempts[-1] - attempts[-2] >= LivenessLoop.RECONCILE_MAX_BACKOFF, \
        "the first episode never reached the maximum backoff — the test proves nothing"

    state.store.set_setting(RW_PENDING_KEY, "")   # the operator (or a retry) finishes it
    loop._reconcile_tick()                        # ...and the loop observes the episode end
    now[0] += DEFAULT_INTERVAL

    state.store.set_setting(RW_PENDING_KEY, "1")  # a NEW revocation is interrupted
    before = len(attempts)
    loop._reconcile_tick()

    assert len(attempts) == before + 1, \
        "the new revocation inherited the finished episode's deadline and was not acted on"


# --- F6: a stop that did not happen may not be reported as a stop -------------------------


def test_a_revocation_that_cannot_stop_xray_says_so_instead_of_claiming_a_stop(settings,
                                                                              stub_xray,
                                                                              monkeypatch):
    """The fail-safe stop is the last branch of a revocation, and it can fail.

    Everything on this path already knew: the log line said xray survived SIGKILL, the incident
    event said it, and then the value handed back to the screen was `stopped` — so the operator
    who had just deleted a lost phone was told remote access was down for everyone while the
    process was still up on the config that names the phone. `stop_net` does not cover it either:
    it governs forwarded client-to-WAN traffic, and the remote-access inbound accepts connections
    TO the gateway.

    Driven with a child that outlives both signals and a config that cannot be rewritten — the
    two conditions that put the revocation on this branch and keep it there.
    """
    from test_supervisor_audit import _Unkillable

    from pi_gw_panel.api import routes

    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, _kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)      # nothing to rebuild from; xray stays up
    monkeypatch.setattr(routes, "_rw_write_config", lambda _state: False)
    state = c.app.state.app_state
    state.supervisor._proc = _Unkillable()

    body = c.delete(f"/api/rw/clients/{lost}", headers=h).json()

    assert body["revocation"] == "stop-failed", \
        "a stop that did not happen was reported to the operator as 'stopped'"
    assert state.supervisor.status()["running"] is True, "the fixture did not survive the stop"
    assert lost in _live_client_ids(settings), \
        "the config was cleaned after all — this test is no longer about a live credential"
    assert any("tried and failed to stop xray" in d for d in _rw_events(c))
    # ...and the revocation is NOT finished, so the recovery has to keep at it.
    assert _pending(c), \
        "an unconfirmed stop cleared the marker, so nothing will ever retry the revocation"


def test_an_unconfirmed_stop_is_retried_until_xray_is_actually_gone(settings, stub_xray,
                                                                   monkeypatch):
    """`stop-failed` is a state to get out of, not one to report and forget.

    "Retry until xray is confirmed stopped OR rebuilt" is two conditions, and cleaning the file
    is only the first. A process that survived SIGKILL is still serving the configuration it
    loaded, so a rewritten file it never reloaded changes nothing about who can connect — the
    marker has to outlive that too, and clear only once the process is actually gone.
    """
    from test_supervisor_audit import _Unkillable

    from pi_gw_panel.api import routes

    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    monkeypatch.setattr(routes, "_rw_write_config", lambda _state: False)
    state = c.app.state.app_state
    state.supervisor._proc = _Unkillable()
    assert c.delete(f"/api/rw/clients/{lost}", headers=h).json()["revocation"] == "stop-failed"

    _liveness_reconcile(c)                       # still stuck: same marker, same retry pending
    assert _pending(c), "the retry gave up on a revocation that had not been applied"

    monkeypatch.undo()                           # whatever blocked the rewrite clears
    _liveness_reconcile(c)
    assert _live_client_ids(settings) == [kept], \
        "the retry never cleaned the config the unkillable xray came up on"
    assert _pending(c), \
        "a rewritten file the unkillable process never reloaded was taken for a finished " \
        "revocation — it is still serving the credential from memory"

    state.supervisor._proc = None                # the stuck child is finally reaped
    _liveness_reconcile(c)

    assert not _pending(c), "a finished revocation left its marker behind"
    assert _live_client_ids(settings) == [kept]


# --- F7: a CLEAN FILE is not a stopped process -------------------------------------------
#
# The revocation asked the config on disk whether anything was live and answered `not-live` when
# it found no inbound. The file is the right evidence about the NEXT start and no evidence at all
# about the current one: an irreversible reapply writes the new config BEFORE it reloads, so a
# reapply that wrote a clean file and then could not make the old process take it leaves exactly
# that state — a clean file in front of a process still serving every credential it loaded. The
# two ways to reach it are the two that rebuild to a config with no `rw-in` at all: turning the
# feature off, and removing or suspending the last enabled client.
#
# Both are answered here with a child that outlives SIGKILL, which is what makes the reload fail
# and keeps the old process up. `not-live` now needs a supervisor observed to be DOWN; anything
# else needs a confirmed reload or a confirmed stop before the marker may be cleared.


def _unkillable_running(state):
    """Leave the supervisor holding a child that cannot be stopped, so every reload fails and
    every stop reports it. Returns the stand-in."""
    from test_supervisor_audit import _Unkillable

    stuck = _Unkillable()
    state.supervisor._proc = stuck
    return stuck


def test_turning_the_feature_off_does_not_call_a_surviving_xray_not_live(settings, stub_xray):
    """PUT /rw {enabled: false} with a node CONNECTED and an xray that will not die.

    The rebuild renders a config with no remote-access inbound and writes it, then fails at the
    reload because the old process cannot be stopped. Reading the file back then said "there was
    nothing live to cut" — and cleared the durable marker, so nothing would ever come back to it —
    about a process that is still up and still accepting every client it loaded.
    """
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    from test_rw_inbound import RW_ARMED
    _nid, lost, kept = _armed_with_two_clients(c, h)
    state = c.app.state.app_state
    _unkillable_running(state)

    body = c.put("/api/rw", json={**RW_ARMED, "enabled": False, "private_key": ""},
                 headers=h).json()

    assert body["revocation"] != "not-live", \
        "a running xray that could not be stopped was reported as nothing being live"
    assert body["revocation"] == "stop-failed"
    assert state.supervisor.status()["running"] is True, "the fixture did not survive the stop"
    assert _pending(c), \
        "the marker was cleared while the process still serving the credential is up, so " \
        "nothing will ever finish this revocation"

    state.supervisor._proc = None                 # the stuck child is finally reaped
    _liveness_reconcile(c)

    assert not _pending(c), "the recovery could not finish it once the process was gone"
    assert _live_client_ids(settings) == [] and lost and kept


def test_removing_the_last_client_does_not_call_a_surviving_xray_not_live(settings, stub_xray):
    """The same hole through the other door: with the last enabled client gone the inbound is not
    emitted at all, so the rebuilt config carries no `rw-in` and the file reads as clean while the
    process that outlived the reload still accepts the client that was just deleted."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    _nid, lost, kept = _armed_with_two_clients(c, h)
    assert c.delete(f"/api/rw/clients/{kept}", headers=h).json()["revocation"] == "reapplied"
    assert _live_client_ids(settings) == [lost]
    state = c.app.state.app_state
    _unkillable_running(state)

    body = c.delete(f"/api/rw/clients/{lost}", headers=h).json()

    assert body["revocation"] != "not-live", \
        "removing the last client left a surviving xray reported as nothing being live"
    assert body["revocation"] == "stop-failed"
    assert _pending(c), "the marker was cleared while the revoked client is still being served"

    state.supervisor._proc = None
    _liveness_reconcile(c)

    assert not _pending(c)
    assert _live_client_ids(settings) == []


def test_a_clean_file_in_front_of_a_running_xray_is_reloaded_before_it_is_believed(settings,
                                                                                   stub_xray):
    """The other half of the same rule, on a process that CAN be reloaded.

    With xray running and the file already clean the revocation may still not answer from the
    file — but there is a way to make the answer true rather than merely hoped for, and it is the
    reload itself. `reload_if_running` decides under the supervisor's own lock, so it cannot start
    a process that went away in the meantime; what it reports is that the live process is now on
    the config we just read.
    """
    from test_rw_inbound import RW_ARMED

    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    for cid in (kept, lost):        # both gone: `resolve()` emits no inbound, so the file goes clean
        c.delete(f"/api/rw/clients/{cid}", headers=h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)          # xray stays up, nothing to rebuild
    with open(settings.config_path) as f:
        assert not any(i.get("tag") == "rw-in" for i in json.load(f)["inbounds"]), \
            "the file is not clean — this test is about the branch that reads it as clean"
    assert c.get("/api/status").json()["running"] is True

    # A narrowing save against an already-clean file: nothing to write, and a live process that
    # may still be on an older config. The only way to an honest answer is to make it true.
    body = c.put("/api/rw", json={**RW_ARMED, "enabled": False, "private_key": ""},
                 headers=h).json()

    assert body["revocation"] == "rebuilt", \
        "the running process was left on whatever it had loaded, on the word of the file"
    assert c.get("/api/status").json()["running"] is True, "the revocation stopped a live tunnel"
    assert _live_client_ids(settings) == []
    assert not _pending(c), "a confirmed reload is a finished revocation"


# --- F8: a RETRY is not the request it is finishing --------------------------------------
#
# The recovery used to re-run the whole revocation, which is far more than the runtime half: an
# irreversible reapply that restarts xray and rewrites the session bookkeeping, an unconditional
# drop of the rollback target, and an incident event per attempt. None of that is idempotent, and
# the retries are paced to repeat for as long as the marker is stuck.


def _fail_marker_clear(store):
    """Make the CLEAR of the marker fail the way a full disk does, leaving the set that opened the
    window intact. Returns a restore callable."""
    from pi_gw_panel.api.routes import RW_PENDING_KEY

    real_set = store.set_setting

    def _failing(name, value):
        if name == RW_PENDING_KEY and not value:
            raise sqlite3.OperationalError("disk I/O error")
        return real_set(name, value)

    store.set_setting = _failing
    return lambda: setattr(store, "set_setting", real_set)


def test_a_retry_keeps_a_rollback_target_published_after_the_marker(settings, stub_xray,
                                                                    monkeypatch):
    """The rollback target an ordinary apply filed AFTER the marker was set.

    Dropping it is right for the revocation itself — the snapshot on file is by construction from
    before it — and false for every retry after it. Here the revocation completed, the marker
    could not be cleared, and an ordinary apply then published an undo target that grants nothing
    revoked. Re-running the revocation destroyed it, together with the session baselines the
    Dashboard measures "this session" from, on every pass for as long as the marker was stuck.
    """
    from pi_gw_panel.api import routes
    from pi_gw_panel.api.routes import RW_PENDING_KEY

    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    state = c.app.state.app_state
    store = state.store

    # A revocation that finished and whose marker would not clear.
    restore = _fail_marker_clear(store)
    try:
        assert c.delete(f"/api/rw/clients/{lost}", headers=h).json()["revocation"] == "reapplied"
        assert c.delete(f"/api/rw/clients/{kept}", headers=h).json()["revocation"] == "reapplied"
    finally:
        restore()
    assert _pending(c), "the marker cleared after all — there is no stuck episode to retry"
    assert _live_client_ids(settings) == []

    # ...and an ordinary apply afterwards, which files the config it replaces as the undo target.
    # That config is post-revocation: it grants nothing the store does not.
    assert c.post(f"/api/nodes/{nid}/apply", headers=h).status_code == 200
    assert c.get("/api/status").json()["rollback_available"] is True, \
        "the apply published no promotable target — there is nothing for the retry to destroy"
    assert not _pending(c), \
        "a full apply rebuilt the config from the store and reloaded onto it, which is the whole " \
        "of what the marker was waiting for — leaving it set reloads a healthy tunnel every tick"

    # The marker is forced back on: the retry itself must be harmless, not merely unreached.
    store.set_setting(RW_PENDING_KEY, "1")
    store.set_setting("session_base_up", "12345")
    store.set_setting("data_used_up", "99999")     # what a reapply would overwrite the base with
    reapplied: list[int] = []
    monkeypatch.setattr(routes, "reapply_active_node",
                        lambda *a, **kw: reapplied.append(1))

    _liveness_reconcile(c)

    assert not reapplied, "the retry ran the reconnecting rebuild that belongs to the request"
    assert store.get_setting("session_base_up") == "12345", \
        "the retry reset the traffic baseline the Dashboard measures a session from"
    assert c.get("/api/status").json()["rollback_available"] is True, \
        "the retry destroyed an undo target that grants nothing the store does not"
    assert not _pending(c), "the retry did not finish a revocation that was already applied"


def test_a_retry_still_drops_a_rollback_target_that_grants_what_was_revoked(settings, stub_xray,
                                                                            monkeypatch):
    """...and the question is asked, not assumed away in the other direction.

    Keeping a post-marker target is only safe for one that agrees with the store. A pairing whose
    snapshot still names the revoked device is exactly what a revocation exists to take away, and
    a retry that cannot prove otherwise must drop it — "we could not tell" is not permission to
    leave a promotable pre-revocation config on disk.
    """
    from pi_gw_panel.api.routes import RW_PENDING_KEY

    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    _stale_pre_revocation_target(settings, stub_xray)      # promotable, and it names `lost`
    monkeypatch.setattr("pi_gw_panel.api.routes.ConfigManager.invalidate_rollback",
                        lambda self: False)                # the request's own sweep cannot
    assert c.delete(f"/api/rw/clients/{lost}", headers=h).json() and _pending(c) is False
    monkeypatch.undo()
    assert c.get("/api/status").json()["rollback_available"] is True, \
        "the stale pre-revocation target was already gone — the leak was never set up"

    c.app.state.app_state.store.set_setting(RW_PENDING_KEY, "1")
    _liveness_reconcile(c)

    assert c.get("/api/status").json()["rollback_available"] is False, \
        "the retry kept a promotable snapshot that still grants the revoked credential"
    assert kept


def test_a_reconciliation_whose_marker_will_not_clear_is_not_reported_as_done(settings, stub_xray):
    """`done` is what the liveness loop resets its backoff and its once-per-episode event on.

    Reading it off the outcome alone meant a reconciliation whose runtime half succeeded and whose
    marker write failed declared victory, the loop forgot the episode, and the next tick ran the
    whole thing again — reloading the live tunnel every 20 seconds for as long as the settings
    write kept failing. The marker is read back now, and nothing short of seeing it gone counts.
    """
    from pi_gw_panel.api.routes import RW_PENDING_KEY, rw_reconcile_pending
    from pi_gw_panel.health.liveness import LivenessLoop

    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    monkeypatch = pytest.MonkeyPatch()
    _nid, lost, kept = _armed_and_interrupted(
        c, h, monkeypatch, lambda cid: c.delete(f"/api/rw/clients/{cid}", headers=h))
    state = c.app.state.app_state

    restore = _fail_marker_clear(state.store)
    try:
        done = rw_reconcile_pending(state)
        loop = LivenessLoop(state)
        loop._reconcile_tick()
    finally:
        restore()

    assert done is False, \
        "a reconciliation that could not clear its marker reported the episode as finished"
    assert _live_client_ids(settings) == [kept], "the runtime half did not actually run"
    assert lost not in _live_client_ids(settings)
    assert _pending(c), "the marker cleared after all — the failure never fired"
    assert loop._reconcile_attempts == 1, \
        "the loop reset its backoff on an episode that is still pending, so the next tick " \
        "repeats the whole reconciliation immediately"

    # ...and a write that fails SILENTLY is the same answer. Nothing about a settings call that
    # returned tells us the key is gone, so the only thing that ends an episode is reading it
    # back absent.
    real_set = state.store.set_setting
    state.store.set_setting = lambda name, value: (
        None if name == RW_PENDING_KEY and not value else real_set(name, value))
    try:
        assert rw_reconcile_pending(state) is False, \
            "a clear that quietly wrote nothing was taken for a finished episode"
    finally:
        state.store.set_setting = real_set
    assert _pending(c)


def test_the_real_reconciler_files_one_event_per_episode_not_one_per_attempt(settings, stub_xray,
                                                                             monkeypatch):
    """The 40-entry connection log is what an operator reads to work out what the box did with a
    lost device, and a stuck episode was flushing it.

    The loop reports the episode exactly once, and that was taken as the whole story — but the
    reconciler underneath it filed a rebuild event and a stop event on every attempt, so two
    retries left four entries behind. The test that claimed otherwise injected a no-op reconciler
    and could not have seen it; this one drives the REAL one, which is the only way the claim
    means anything.
    """
    from pi_gw_panel.api import routes
    from pi_gw_panel.health.liveness import DEFAULT_INTERVAL, LivenessLoop

    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, _kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    monkeypatch.setattr(routes, "_rw_write_config", lambda _state: False)
    state = c.app.state.app_state
    _unkillable_running(state)
    assert c.delete(f"/api/rw/clients/{lost}", headers=h).json()["revocation"] == "stop-failed"
    before = len(_rw_events(c))

    now = [1000.0]
    loop = LivenessLoop(state, now=lambda: now[0])       # the REAL reconciler, not a stand-in
    for _ in range(2):
        loop._reconcile_tick()
        now[0] += 10 * DEFAULT_INTERVAL                  # past every backoff window

    assert loop._reconcile_attempts == 2, "the two retries did not actually run"
    assert _pending(c), "the episode resolved — there was nothing to retry"
    assert len(_rw_events(c)) - before == 1, \
        f"two retries filed {len(_rw_events(c)) - before} entries in a 40-deep log; the episode " \
        "is reported once and the attempts belong in the ordinary log"
    assert any("did not reach the running xray" in d for d in _rw_events(c)), \
        "the one entry that survived is not the episode report"


# --- F9: only whoever holds the lock may say a revocation is finished --------------------
#
# Completing the marker on a successful apply is right, and it was done in the wrong PLACE: a
# wrapper around selected callers, i.e. after `apply_node` had already released `apply_lock`. Two
# faults, one cause.
#
#   * A revocation can commit a NEWER marker in that gap and then fail to prove the credential is
#     gone, and the older apply's wrapper erased that marker afterwards — leaving nothing to ever
#     come back to a revocation whose device is still being served.
#   * Wrapping callers covers the callers somebody wrapped. The boot reapply, a subscription
#     refresh and the failover tick reach the same proven success off the request path entirely.
#
# Both are answered by moving the completion into `apply_node`'s locked success path, which is
# where the proof exists and where it cannot be interleaved.


def _marker(store) -> str:
    from pi_gw_panel.controller import RW_PENDING_KEY
    return store.get_setting(RW_PENDING_KEY) or ""


def _wait_for_marker(store, value: str, timeout: float) -> bool:
    """Whether the marker becomes exactly `value` within `timeout`. Read-only, so it can watch
    another thread without writing anything the store has to serialize against."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _marker(store) == value:
            return True
        time.sleep(0.01)
    return False


def test_an_older_apply_does_not_erase_a_newer_revocation_s_marker(settings, stub_xray):
    """Two threads and one barrier: an apply that has succeeded, and a revocation behind it.

    The apply is held at the exact instant it completes the marker. While it waits there, a
    revocation is asked for — one that cannot confirm its stop, so it MUST leave a marker behind.
    Whether that marker survives is decided entirely by whether the apply is still holding
    `apply_lock` when it clears: from inside the lock the revocation cannot even begin, and the
    apply can only ever erase the episode it actually finished; from outside it, the revocation
    runs to completion in the gap and the apply erases a marker that describes a credential still
    being served.

    The two markers are told apart by their VALUE — the older episode is tagged, a revocation
    always writes "1" — so the final assertion is about which marker is on disk, not merely about
    one being there.
    """
    from pi_gw_panel.controller import RW_PENDING_KEY, apply_node

    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    state = c.app.state.app_state
    store = state.store
    store.set_setting(RW_PENDING_KEY, "an-earlier-episode")   # what the apply is entitled to clear

    at_clear, release = threading.Event(), threading.Event()
    real_set = store.set_setting

    def _hold_the_apply_at_its_clear(name, value):
        if name == RW_PENDING_KEY and not value and not at_clear.is_set():
            at_clear.set()
            release.wait(30)      # the main thread releases it; the timeout only bounds a hang
        return real_set(name, value)

    store.set_setting = _hold_the_apply_at_its_clear
    applied, revoked = [], []
    applying = threading.Thread(target=lambda: applied.append(
        apply_node(store.get_node(nid), state.settings, state.supervisor, state.net,
                   store=store, xray_bin=state.xray_bin)))
    revoking = threading.Thread(target=lambda: revoked.append(
        c.delete(f"/api/rw/clients/{lost}", headers=h).json()))
    try:
        applying.start()
        assert at_clear.wait(30), "the apply never tried to complete the pending marker"
        _unkillable_running(state)        # from here nothing can confirm a stop
        revoking.start()
        # Did the revocation get its own marker in while the apply was still finishing? Under the
        # lock it cannot: it blocks before the deletion, so this waits out its window instead.
        interleaved = _wait_for_marker(store, "1", 1.5)
    finally:
        release.set()
        applying.join(30)
        revoking.join(30)
        store.set_setting = real_set

    assert applied and applied[0].ok, "the apply under test failed — it clears nothing either way"
    assert revoked and revoked[0]["revocation"] == "stop-failed", \
        "the revocation confirmed its runtime half, so it left no marker to be erased"
    assert not interleaved, \
        "a revocation committed a marker while an apply was still completing one: the apply is " \
        "clearing outside `apply_lock` again, which is the whole of the window"
    assert _marker(store) == "1", \
        "the older apply erased the marker of a newer revocation that could not confirm its " \
        "stop, so nothing will ever come back to finish it while the credential is still served"
    assert kept


@pytest.mark.parametrize("caller", ["boot-reapply", "subscription-refresh", "failover-promote"])
def test_a_full_apply_off_the_request_path_also_completes_the_marker(settings, stub_xray, caller):
    """The same proven success, reached by the callers no wrapper covered.

    A full apply rebuilds the config from the store and reloads xray onto it — that is what ends a
    pending revocation, and it is true of the apply, not of the route that asked for it. While the
    completion lived in a wrapper around selected handlers, `app.py`'s boot reapply, the
    subscription refresh in `subs/service.py` and the failover promotion in `health/failover.py`
    left the marker set on a success: a needless incident report, a needless reload of a healthy
    tunnel on the next tick, and a retry that then destroyed the fresh rollback target that same
    apply had just published.

    Each case here is the call those paths actually make — the boot one through
    `reapply_active_node` itself, the others through `apply_node` with the argument shape each site
    passes (the failover tick notably passes no `xray_bin`).
    """
    from pi_gw_panel.controller import RW_PENDING_KEY, apply_node, reapply_active_node

    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    state = c.app.state.app_state
    store = state.store
    store.set_setting(RW_PENDING_KEY, "1")

    if caller == "boot-reapply":
        res = reapply_active_node(state)
    elif caller == "subscription-refresh":
        res = apply_node(store.get_node(nid), state.settings, state.supervisor, state.net,
                         store=store, xray_bin=state.xray_bin)
    else:
        res = apply_node(store.get_node(nid), state.settings, state.supervisor, state.net,
                         store=store)

    assert res is not None and res.ok, f"the {caller} apply failed: {res and res.error}"
    assert not _pending(c), \
        f"a successful {caller} left the marker set, so the next tick reloads a healthy tunnel " \
        "and the retry drops the rollback target this apply published"
    assert sorted(_live_client_ids(settings)) == sorted([lost, kept]), \
        "the apply did not rebuild the config from the store — its success proves nothing"


def test_an_apply_that_fails_leaves_the_marker_exactly_where_it_was(settings, stub_xray,
                                                                    monkeypatch):
    """The other half of centralizing it: only a SUCCESS is proof.

    A failed apply may have stopped at the config, at the reload or at the net rules, and may have
    left the old process up on the old file — which is precisely the state the marker records. So
    the marker is not something an apply clears on its way past; it is cleared by an apply that got
    all the way through, and by nothing else.
    """
    from pi_gw_panel.controller import RW_PENDING_KEY, apply_node

    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, _lost, _kept = _armed_with_two_clients(c, h)
    state = c.app.state.app_state
    state.store.set_setting(RW_PENDING_KEY, "1")

    monkeypatch.setenv("STUB_XRAY_FAIL", "1")            # nothing this apply builds can validate
    res = apply_node(state.store.get_node(nid), state.settings, state.supervisor, state.net,
                     store=state.store, xray_bin=state.xray_bin)
    monkeypatch.undo()

    assert res is not None and not res.ok, "the apply was supposed to fail"
    assert _pending(c), \
        "a failed apply cleared the marker: the revocation is now unfinished and unrecorded"


# --- F10: a retry may not destroy the undo it is about to try to preserve ----------------
#
# `_rw_drop_unsafe_rollback` asks whether the rollback target still grants what was revoked, so a
# legitimate one published after the marker survives. It could never find one on the writing path:
# the runtime push runs first, and `apply_irreversible` durably invalidates the pairing before it
# touches the file — even when the config it was about to write is the one already on disk.


def test_a_retry_does_not_rewrite_a_config_that_already_agrees_with_the_store(settings, stub_xray,
                                                                             monkeypatch):
    """One client still enabled, so the reconcile goes down the path that WRITES.

    Its sibling above deletes both clients, which leaves no `rw-in` in the file at all and takes
    the branch that writes nothing — so it could not have seen this. Here `kept` is still granted,
    the file still carries the inbound, and the reconcile therefore reaches `_rw_write_config`
    against a config an ordinary apply has already brought into line with the store. There is
    nothing to write, and writing it anyway cost the operator the undo that apply had published.
    """
    from pi_gw_panel.api import routes
    from pi_gw_panel.api.routes import RW_PENDING_KEY

    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    store = c.app.state.app_state.store

    # A revocation that finished and whose marker would not clear, with `kept` still enabled.
    restore = _fail_marker_clear(store)
    try:
        assert c.delete(f"/api/rw/clients/{lost}", headers=h).json()["revocation"] == "reapplied"
    finally:
        restore()
    assert _pending(c), "the marker cleared after all — there is no stuck episode to retry"
    assert _live_client_ids(settings) == [kept], "the revocation did not reach the config"

    # ...and an ordinary apply afterwards, which files the config it replaces as the undo target.
    # That config is post-revocation: it grants nothing the store does not.
    assert c.post(f"/api/nodes/{nid}/apply", headers=h).status_code == 200
    assert c.get("/api/status").json()["rollback_available"] is True, \
        "the apply published no promotable target — there is nothing for the retry to preserve"
    with open(settings.config_path) as f:
        before = json.load(f)

    store.set_setting(RW_PENDING_KEY, "1")          # the stuck marker, forced back on
    wrote: list[bool] = []
    real_write = routes._rw_write_config

    def _watched(state):
        wrote.append(True)
        return real_write(state)

    monkeypatch.setattr(routes, "_rw_write_config", _watched)
    _liveness_reconcile(c)
    monkeypatch.undo()

    assert wrote, "the reconcile never reached the writing path — this is the no-inbound test again"
    with open(settings.config_path) as f:
        assert json.load(f) == before, \
            "the reconcile rewrote a live config that already granted exactly what the store does"
    assert c.get("/api/status").json()["rollback_available"] is True, \
        "the retry destroyed the undo target an ordinary apply had published, by writing a config " \
        "identical to the one already on disk"
    assert _live_client_ids(settings) == [kept], "the retry changed what the config grants"
    assert not _pending(c), "the reconcile did not finish an episode whose work was already done"


def test_a_retry_still_writes_when_the_file_and_the_store_disagree(settings, stub_xray):
    """...and the delta is measured, not assumed, in the direction that matters more.

    Skipping a write that WAS needed is the worse failure — it leaves a credential in the file for
    the next start to serve. So the skip is keyed on a content digest of what would be written
    against what is there, and a file that differs by so much as one client is rewritten. Here the
    revocation is interrupted before its runtime half runs at all, so the file still names the
    revoked device and the retry has real work to do.
    """
    monkeypatch = pytest.MonkeyPatch()
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    _nid, lost, kept = _armed_and_interrupted(
        c, h, monkeypatch, lambda cid: c.delete(f"/api/rw/clients/{cid}", headers=h))
    assert lost in _live_client_ids(settings), "the fixture left nothing for the retry to write"

    _liveness_reconcile(c)

    assert _live_client_ids(settings) == [kept], \
        "the retry skipped a write the file actually needed and left the revoked uuid in it"
    assert not _pending(c)


# --- F9: ONE definition of the pending-revocation key ----------------------------------------
#
# The recovery loop is the only thing that ever comes back for a revocation whose runtime half
# never happened, and it finds one by reading a single settings key. The controller sets and
# clears that key. A second literal for it in `liveness` therefore does not misbehave today and
# cannot be seen to: it is a rename away from a loop that reads a name nothing writes any more,
# sees nothing pending forever, and leaves the running xray serving a credential the store has
# already taken away — with the watchdog passing over it, because that process is healthy.


def test_liveness_and_the_controller_name_the_same_pending_marker():
    """The key has one definition, and `liveness` gets it from the controller.

    Two assertions because they fail on different mistakes. Equality catches a divergence — a
    renamed key with a stale copy left behind. The source check catches the DUPLICATE itself,
    which is the state that precedes every such divergence and which no behavioural test can see
    while the two literals still agree: `from ... import RW_PENDING_KEY` binds the controller's
    value at import, so a second definition here is indistinguishable at runtime right up to the
    moment somebody changes one of them.
    """
    from pathlib import Path

    from pi_gw_panel import controller
    from pi_gw_panel.health import liveness

    assert liveness.RW_PENDING_KEY == controller.RW_PENDING_KEY
    src = Path(liveness.__file__).read_text()
    assert "RW_PENDING_KEY =" not in src, \
        "liveness restated the pending-marker key instead of importing the controller's"
    assert "import" in next(line for line in src.splitlines() if "RW_PENDING_KEY" in line), \
        "the first mention of the key in liveness is not its import"


def test_the_recovery_tick_reads_the_key_the_controller_defines():
    """...and it is that key the tick actually looks up, not merely one it agrees with.

    The marker is written under the controller's name only. If the loop read anything else, it
    would find nothing pending and the reconcile would never run — the silent failure this
    de-duplication exists to make impossible.
    """
    from pi_gw_panel.controller import RW_PENDING_KEY
    from pi_gw_panel.db import connect, init_schema
    from pi_gw_panel.health.liveness import LivenessLoop
    from pi_gw_panel.nodes.store import NodeStore

    conn = connect(":memory:")
    init_schema(conn)
    store = NodeStore(conn)

    class _St:
        pass

    st = _St()
    st.store = store
    store.set_setting(RW_PENDING_KEY, "1")

    read: list[str] = []
    real_get = store.get_setting

    def watching_get(name):
        read.append(name)
        return real_get(name)

    store.get_setting = watching_get

    attempts: list[int] = []
    loop = LivenessLoop(st, now=lambda: 1000.0,
                        reconcile=lambda _st: bool(attempts.append(1)) or False)
    loop._reconcile_tick()

    assert RW_PENDING_KEY in read, \
        "the tick looked up some other key for the pending marker"
    assert attempts, "the marker the controller writes did not trigger the recovery"
