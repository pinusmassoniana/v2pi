"""Regression cover for the config-manager / supervisor / traffic-WS audit fixes.

Each test here fails if the specific defect it names comes back.
"""
import asyncio
import json
import os
import subprocess

import pytest
from fastapi.testclient import TestClient

from pi_gw_panel import app as app_mod
from pi_gw_panel.app import create_app
from pi_gw_panel.auth.tokens import SCOPES
from pi_gw_panel.controller import apply_node
from pi_gw_panel.db import connect, init_schema, migrate
from pi_gw_panel.models import Node
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.state import build_state
from pi_gw_panel.xray_config.validate import ConfigManager, scrub_output
from pi_gw_panel.xray_supervisor.supervisor import XraySupervisor


def _wire(settings, stub_xray):
    conn = connect(settings.db_path)
    init_schema(conn)
    store = NodeStore(conn)
    nid = store.add_node(Node(id=None, name="n1", address="1.2.3.4", port=47000,
                              uuid="u-1", sni="www.microsoft.com",
                              public_key="PK", short_id="ab12"))
    return store, nid, XraySupervisor(xray_bin=stub_xray,
                                      config_path=settings.config_path), DryRunBackend()


# --- F4-09: a damaged live config must not wedge every future apply -------------------

@pytest.mark.parametrize("damage", ['{"outbounds": [', "", "   ", "not json at all"])
def test_apply_survives_an_unreadable_live_config(settings, stub_xray, damage):
    """The live config is snapshotted as the rollback target before a new one is written.
    A truncated/hand-edited config.json used to raise out of apply(), so EVERY subsequent
    apply failed with 'config apply failed' — repairable only over SSH."""
    mgr = ConfigManager(settings, xray_bin=stub_xray)
    assert mgr.apply({"v": "first"})[0] is True
    assert mgr.apply({"v": "second"})[0] is True     # snapshots "first" as last-good
    with open(settings.config_path, "w") as f:
        f.write(damage)

    ok, out = mgr.apply({"v": "third"})

    assert ok, out
    with open(settings.config_path) as f:
        assert json.load(f) == {"v": "third"}
    # the intact earlier snapshot is preserved rather than overwritten with the damage
    with open(settings.lastgood_path) as f:
        assert json.load(f) == {"v": "first"}
    # ...but it is NOT this apply's undo: "first" is not what "third" replaced. Keeping the file
    # as a repair artifact and refusing to promote it automatically are two different things —
    # see tests/test_validate.py for the provenance contract this now fails closed on.
    assert mgr.rollback() is False


def test_apply_node_recovers_from_a_truncated_live_config(settings, stub_xray):
    """Same defect seen from the caller: the raise landed before apply_node's rollback
    block, so a corrupt config.json turned every Connect into a 502."""
    store, nid, sup, net = _wire(settings, stub_xray)
    try:
        assert apply_node(store.get_node(nid), settings, sup, net,
                          store=store, xray_bin=stub_xray).ok is True
        sup.stop()
        with open(settings.config_path, "w") as f:
            f.write('{"outbounds')

        res = apply_node(store.get_node(nid), settings, sup, net,
                         store=store, xray_bin=stub_xray)

        assert res.ok is True, res.error
        with open(settings.config_path) as f:
            assert json.load(f)["outbounds"][0]["settings"]["vnext"][0]["address"] == "1.2.3.4"
    finally:
        sup.stop()


# --- F4-10: durability of the atomic write --------------------------------------------

def test_write_atomic_fsyncs_contents_before_replace_and_dir_after(tmp_path, monkeypatch):
    """os.replace is atomic for the *rename* only. Without an fsync a power cut can leave
    config.json/lastgood zero-length — xray then won't start at boot."""
    calls: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(
        os, "replace", lambda src, dst: (calls.append("replace"), real_replace(src, dst))[1])

    target = tmp_path / "sub" / "config.json"
    ConfigManager._write_atomic(str(target), {"a": 1})

    assert json.loads(target.read_text()) == {"a": 1}
    assert "replace" in calls, "the write is no longer going through a temp file + rename"
    assert calls.index("fsync") < calls.index("replace"), \
        "contents must be durable BEFORE the rename that publishes them"
    assert calls[-1] == "fsync", "the directory entry created by the rename must be fsynced too"


# --- F4-11: redaction of list-valued secrets, and redaction that stays on --------------

def _rw_config() -> dict:
    return {"inbounds": [{
        "tag": "rw-in",
        "settings": {"clients": [{"id": "11111111-2222-3333-4444-555555555555"}]},
        "streamSettings": {"realitySettings": {
            "privateKey": "PRIVKEYVALUE",
            "shortIds": ["deadbeefcafe1234", "ab12"],
        }},
    }]}


def test_scrub_output_redacts_list_valued_short_ids():
    """`shortIds` is a LIST — the collector only ever added str values, so every
    road-warrior short id leaked through `xray -test` output and /api/status.last_error."""
    text = ("failed to start inbound rw-in: shortIds [deadbeefcafe1234 ab12] "
            "privateKey PRIVKEYVALUE client 11111111-2222-3333-4444-555555555555")

    out = scrub_output(text, _rw_config())

    for secret in ("deadbeefcafe1234", "ab12", "PRIVKEYVALUE",
                   "11111111-2222-3333-4444-555555555555"):
        assert secret not in out, f"{secret!r} survived redaction"


def test_supervisor_keeps_redacting_when_the_config_becomes_unreadable(settings, stub_xray):
    """An unreadable config used to reset the redaction vocabulary to {}, switching scrubbing
    off wholesale — exactly when xray is loudest about the secrets it was given."""
    with open(settings.config_path, "w") as f:
        json.dump(_rw_config(), f)
    sup = XraySupervisor(xray_bin=stub_xray, config_path=settings.config_path)
    try:
        sup.start()
        sup.stop()
        with open(settings.config_path, "w") as f:
            f.write('{"inbounds": [')   # truncated by a power cut / mid-write crash
        sup.start()
        with sup._stderr_lock:
            sup._stderr_tail = ("reality: bad shortId deadbeefcafe1234, "
                                "privateKey PRIVKEYVALUE")

        last_error = sup.status()["last_error"]

        assert "deadbeefcafe1234" not in last_error
        assert "PRIVKEYVALUE" not in last_error
        assert "***" in last_error
    finally:
        sup.stop()


# --- F4-15: a build failure is a reported result, not a 500 ---------------------------

def test_apply_node_reports_a_build_failure_instead_of_raising(settings, stub_xray):
    """`build_node_config` renders values that are only validated at the API boundary; a
    hand-edited/restored `stats_api_port` reaches int() and raised straight out of apply_node
    because the build sat outside its try/except."""
    store, nid, sup, net = _wire(settings, stub_xray)
    store.set_setting("stats_api_port", "not-a-port")
    try:
        res = apply_node(store.get_node(nid), settings, sup, net,
                         store=store, xray_bin=stub_xray)

        assert res.ok is False
        assert "config apply failed" in res.error
        assert sup.status()["running"] is False      # nothing was started
        assert net.applied == []                     # nothing was applied to the host
    finally:
        sup.stop()


# --- F1-6: the traffic WS must not block the event loop -------------------------------

def _off_loop_spy(calls: list, name: str, fn):
    def spy(*args, **kwargs):
        try:
            asyncio.get_running_loop()
            calls.append((name, "event-loop"))
        except RuntimeError:
            calls.append((name, "worker-thread"))
        return fn(*args, **kwargs)
    return spy


def test_traffic_ws_does_its_blocking_store_reads_off_the_event_loop(
        settings, stub_xray, monkeypatch):
    """ws_traffic's per-tick session check, settings reads and frame build are all blocking
    SQLite reads that take the store's single connection lock. Run inline they stall the whole
    event loop for as long as a REST handler holds that lock across an xray/nft apply."""
    from pi_gw_panel.api import deps

    settings.xray_bin = stub_xray
    state = build_state(settings, net=DryRunBackend())
    client = TestClient(create_app(settings, state=state))
    client.post("/api/setup", json={"username": "admin", "password": "changeme"})
    state.recorder.record_sample({"proxy": {"up_bps": 1.0, "down_bps": 2.0}})

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(app_mod, "_traffic_frame",
                        _off_loop_spy(calls, "_traffic_frame", app_mod._traffic_frame))
    monkeypatch.setattr(app_mod, "_traffic_settings",
                        _off_loop_spy(calls, "_traffic_settings", app_mod._traffic_settings))
    monkeypatch.setattr(deps, "session_invalid_reason",
                        _off_loop_spy(calls, "session_invalid_reason",
                                      deps.session_invalid_reason))

    with client.websocket_connect("/api/ws/traffic") as ws:
        assert "outbounds" in ws.receive_json()

    assert {name for name, _ in calls} == {
        "_traffic_frame", "_traffic_settings", "session_invalid_reason"}
    on_loop = [name for name, where in calls if where == "event-loop"]
    assert not on_loop, f"blocking store reads ran on the event loop: {sorted(set(on_loop))}"


# --- dead code: one source for the token scope vocabulary -----------------------------

def test_token_scope_check_constraint_tracks_the_scope_vocabulary(tmp_path):
    """db.py used to hand-spell 'monitor','read','readwrite' three times; adding a scope to
    auth.tokens.SCOPES would then create tokens the DB rejects."""
    conn = connect(str(tmp_path / "scopes.sqlite"))
    init_schema(conn)
    migrate(conn)

    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='api_tokens'").fetchone()["sql"]

    for scope in SCOPES:
        assert f"'{scope}'" in sql, f"scope {scope!r} is not accepted by the api_tokens CHECK"


# --- the stop has to be bounded, and a failed stop is not a reason to start ------------

class _Unkillable:
    """A child that outlives both signals — an uninterruptible syscall, a wedged tproxy socket,
    a frozen mount. `wait()` without a timeout is the defect itself, so it fails loudly."""

    returncode = None
    pid = 4242

    def __init__(self):
        self.signals: list[str] = []

    def poll(self):
        return None

    def terminate(self):
        self.signals.append("term")

    def kill(self):
        self.signals.append("kill")

    def wait(self, timeout=None):
        if timeout is None:
            raise AssertionError(
                "the supervisor waited on a killed child with no timeout — a stuck xray parks "
                "the supervisor lock, apply_lock and every caller behind them, forever")
        raise subprocess.TimeoutExpired("xray", timeout)


def test_a_child_that_survives_kill_ends_the_stop_instead_of_waiting_forever(settings, stub_xray):
    """The post-`kill()` wait was unbounded. xray is stopped while `apply_lock` is held — and a
    revocation used to hold the store's single connection across it too — so one unreapable child
    took the panel's whole write path with it, not merely the tunnel. A stop that cannot be
    confirmed is a fact to report in bounded time."""
    sup = XraySupervisor(stub_xray, settings.config_path)
    stuck = _Unkillable()
    sup._proc = stuck

    assert sup.stop() is False, "an unstoppable child was reported as cleanly stopped"
    assert stuck.signals == ["term", "kill"], "the escalation to SIGKILL was skipped"
    assert sup._proc is stuck, \
        "the stuck child was forgotten, so the next start puts a second xray on the same port"
    assert sup.status()["running"] is True, "status denied a process that is demonstrably alive"


def test_a_stop_that_did_not_happen_is_a_failed_reload_that_starts_nothing(settings, stub_xray):
    """"Could not stop it" may never become "so start another one". The reload reports the failed
    transition and leaves the process it could not stop exactly where it is; `reload_if_running`
    keeps its three answers, answering False (was running, did not confirm) rather than None."""
    sup = XraySupervisor(stub_xray, settings.config_path)
    stuck = _Unkillable()
    sup._proc = stuck

    assert sup.reload() is False, "a reload that could not stop the old process claimed success"
    assert sup._proc is stuck, "the reload started a second xray beside one it could not stop"
    assert sup.reload_if_running() is False, "a failed stop was reported as 'was not running'"
    assert sup._proc is stuck


# --- no spawn may skip the credential guard -------------------------------------------

def test_start_asks_the_guard_and_does_not_spawn_when_it_says_no(settings, stub_xray):
    """Every route that serves a config as found checks it against the store first, and the one
    caller that has no route — the liveness watchdog, restarting a crashed xray — walked around
    all of them with a plain `supervisor.start()`. The check belongs on the spawn."""
    sup = XraySupervisor(stub_xray, settings.config_path)
    answers = [False, True]
    sup.set_start_guard(lambda: answers.pop(0))

    sup.start()
    assert sup.status()["running"] is False, "the guard said no and xray started anyway"
    assert "revoked" in sup.status()["last_error"], "the refusal left no explanation"

    sup.start()
    try:
        assert sup.status()["running"] is True, "a permitted start was refused"
    finally:
        sup.stop()


def test_the_production_wiring_installs_the_credential_guard(settings, stub_xray):
    """The guard is only unavoidable if it is actually wired — and `build_state` is the one
    place that can do it, since the guard needs the finished state the supervisor belongs to."""
    settings.xray_bin = stub_xray
    state = build_state(settings, net=DryRunBackend())
    try:
        assert state.supervisor._start_guard is not None, \
            "the real wiring left the supervisor able to spawn xray unchecked"
        assert state.supervisor._start_guard() is True, "an untouched config was refused"
    finally:
        state.close()


# --- a rollback's recovery stop is an answer, not a gesture ----------------------------
#
# `POST /rollback` has two recovery stops, and both threw the new boolean away. The reload-failed
# branch raised 502 with a message that named only the reload; the no-previous-node branch went
# further and CONTINUED — clearing the active selection and answering {"ok": true} — while the
# process it had failed to kill was still up on the config it had loaded. A rollback is one of
# the two routes that installs a config the store did not produce, so "we took the old process
# down" is the claim it can least afford to get wrong.


def _rollback_client(settings, stub_xray, monkeypatch):
    """A logged-in client whose rollback has a promotable candidate. The candidate grants no
    remote access, so it takes the plain promotion path and the credential guard is not what
    this is testing."""
    from conftest import _client, _login
    monkeypatch.setattr("pi_gw_panel.api.routes.ConfigManager.rollback_target",
                        lambda _self, *, log=False: {"inbounds": [], "outbounds": []})
    monkeypatch.setattr("pi_gw_panel.api.routes.ConfigManager.rollback", lambda _self: True)
    c = _client(settings, stub_xray)
    return c, {"X-CSRF-Token": _login(c)}


def test_a_rollback_whose_reload_fails_reports_the_xray_it_could_not_kill(settings, stub_xray,
                                                                         monkeypatch):
    """The reload-failed branch. `reload()` on an unstoppable child starts nothing and answers
    False, and the recovery stop that follows fails for the same reason — so the 502 said the
    rolled-back xray "did not become ready" when the truth is that the old one never went away
    and is still serving its config."""
    c, h = _rollback_client(settings, stub_xray, monkeypatch)
    state = c.app.state.app_state
    state.store.set_setting("prev_active_node_id", "1")
    stuck = _Unkillable()
    state.supervisor._proc = stuck

    r = c.post("/api/rollback", headers=h)

    assert r.status_code == 502
    assert "survived SIGKILL" in r.json()["detail"], \
        "the 502 described the reload and said nothing about the process still running"
    assert state.supervisor._proc is stuck, "the survivor was forgotten between the two stops"


def test_a_rollback_with_no_previous_node_does_not_call_a_survivor_a_success(settings, stub_xray,
                                                                            monkeypatch):
    """The no-previous-node branch, and the worse half of the defect.

    With nothing to restore the route stops xray and tears the rules down. The stop result was
    dropped, so an unkillable child sailed past it: the active selection was cleared and the
    operator got {"ok": true} while that child was still up on the config it had loaded. The
    fail-closed net guard still has to run — a process we cannot kill is exactly when it matters
    — and only then may the answer be a 502 that names the survivor, with the selection left
    alone because it still describes something that is running.
    """
    c, h = _rollback_client(settings, stub_xray, monkeypatch)
    state = c.app.state.app_state
    state.store.set_setting("active_node_id", "7")
    state.store.set_setting("prev_active_node_id", "")
    state.supervisor._proc = _Unkillable()

    from pi_gw_panel.api import routes
    real_stop_net, stops = routes.stop_net, []
    monkeypatch.setattr(routes, "stop_net",
                        lambda *a, **kw: (stops.append(1), real_stop_net(*a, **kw))[1])

    r = c.post("/api/rollback", headers=h)

    assert r.status_code == 502, "a rollback that left xray running answered {'ok': true}"
    assert "survived SIGKILL" in r.json()["detail"]
    assert state.store.get_setting("active_node_id") == "7", \
        "the active selection was cleared while the tunnel it names is still up"
    assert stops, "the fail-closed net guard was skipped on the way out to the 502"
