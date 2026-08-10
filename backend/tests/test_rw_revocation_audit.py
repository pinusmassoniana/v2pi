"""Revocation defects found by audit: the ways a REVOKED remote-access credential stays usable.

Every test here is written so it fails if one specific defect comes back. They share one shape —
arm the road-warrior inbound with two clients, revoke one, and then ask whether the credential is
really gone from the store, from the config on disk, and from the running process.

The base fixtures and helpers live in `test_rw_inbound.py`; this module imports them rather than
growing a second copy that can drift from the first.
"""
import json
import sqlite3

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

    monkeypatch.setattr("pi_gw_panel.api.routes.ConfigManager.invalidate_rollback",
                        lambda self: False)                 # the sweep reports it could not
    body = c.delete(f"/api/rw/clients/{lost}", headers=h).json()
    monkeypatch.undo()

    assert body["revocation"] == "not-live", "the revocation took a path that writes a config"
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

    monkeypatch.setattr("pi_gw_panel.api.routes.ConfigManager.invalidate_rollback",
                        lambda self: False)             # the sweep reports it could not
    assert c.delete(f"/api/rw/clients/{lost}", headers=h).json()["revocation"] == "not-live"
    monkeypatch.undo()
    assert c.get("/api/status").json()["rollback_available"] is True, \
        "the promotable pre-revocation target was already gone — the leak was never set up"
    with open(settings.config_path) as f:
        before = json.load(f)

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
