"""Regression cover for the config-manager / supervisor / traffic-WS audit fixes.

Each test here fails if the specific defect it names comes back.
"""
import asyncio
import json
import os

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
