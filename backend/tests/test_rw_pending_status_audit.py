"""`GET /rw` reports a revocation that has not reached the running xray (`revocation_pending`).

The marker itself already existed — a settings row committed with every narrowing save, suspend and
delete, and cleared only on proof — but nothing exposed it. A `stop-failed` answer is a toast that
expires, so an operator who reloaded the screen lost the one warning that the device they had just
revoked might still connect. These tests pin the field to the marker for its whole life: set from
the commit, kept across a failed retry and a restart, cleared only by what clears the marker, and
never reported as `false` when the marker cannot be read.

The fixtures and helpers come from the modules that already define them.
"""
import pytest
from conftest import _client, _login
from test_rw_inbound import _armed_with_two_clients
from test_rw_revocation_audit import _liveness_reconcile, _pending


def _stop_failed_delete(c, h, monkeypatch):
    """Delete one of two armed clients while xray cannot be stopped: the revocation answers
    `stop-failed` and leaves the marker set (the setup of the unkillable-child audit test)."""
    from test_supervisor_audit import _Unkillable

    from pi_gw_panel.api import routes

    nid, lost, kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    monkeypatch.setattr(routes, "_rw_write_config", lambda _state: False)
    c.app.state.app_state.supervisor._proc = _Unkillable()
    return nid, lost, kept, c.delete(f"/api/rw/clients/{lost}", headers=h).json()


def test_a_fresh_gateway_has_no_pending_revocation(settings, stub_xray):
    c = _client(settings, stub_xray)
    _login(c)
    assert c.get("/api/rw").json()["revocation_pending"] is False


def test_a_stop_failed_delete_reports_the_pending_revocation_in_its_reply_and_in_get(
        settings, stub_xray, monkeypatch):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    _nid, _lost, _kept, body = _stop_failed_delete(c, h, monkeypatch)

    assert body["revocation"] == "stop-failed"
    assert body["revocation_pending"] is True, \
        "the reply that says the stop could not be confirmed did not say the revocation is pending"
    assert c.get("/api/rw").json()["revocation_pending"] is True
    assert _pending(c)


def test_it_stays_pending_through_a_failed_retry_and_clears_once_the_child_is_gone(
        settings, stub_xray, monkeypatch):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    _stop_failed_delete(c, h, monkeypatch)

    _liveness_reconcile(c)                                 # still stuck: nothing is proven
    assert c.get("/api/rw").json()["revocation_pending"] is True

    monkeypatch.undo()                                     # the rewrite works again...
    _liveness_reconcile(c)
    assert c.get("/api/rw").json()["revocation_pending"] is True, \
        "a rewritten file the unkillable process never reloaded was reported as finished"

    c.app.state.app_state.supervisor._proc = None          # ...and the stuck child is reaped
    _liveness_reconcile(c)
    assert c.get("/api/rw").json()["revocation_pending"] is False


def test_it_survives_a_panel_restart_whose_boot_recovery_cannot_finish(
        settings, stub_xray, monkeypatch):
    from pi_gw_panel.api import routes

    first = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(first)}
    _stop_failed_delete(first, h, monkeypatch)
    first.app.state.app_state.supervisor._proc = None      # this panel is gone
    monkeypatch.undo()

    tried: list[bool] = []

    def _still_stuck(_state):
        tried.append(True)
        return "stop-failed", False

    monkeypatch.setattr(routes, "_rw_reconcile_runtime", _still_stuck)
    with _client(settings, stub_xray) as second:
        login = second.post("/api/login", json={"username": "admin", "password": "changeme"})
        assert login.status_code == 200
        assert tried, "the boot recovery never looked at the marker — this test proves nothing"
        assert second.get("/api/rw").json()["revocation_pending"] is True, \
            "a restart forgot a revocation that had not reached the running xray"


def test_a_successful_full_apply_clears_it(settings, stub_xray):
    from pi_gw_panel.controller import RW_PENDING_KEY

    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, _lost, _kept = _armed_with_two_clients(c, h)
    c.app.state.app_state.store.set_setting(RW_PENDING_KEY, "1")
    assert c.get("/api/rw").json()["revocation_pending"] is True

    assert c.post(f"/api/nodes/{nid}/apply", headers=h).status_code == 200
    assert c.get("/api/rw").json()["revocation_pending"] is False


def test_a_revocation_that_completes_answers_false_in_its_own_reply(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    nid, lost, kept = _armed_with_two_clients(c, h)
    c.post(f"/api/nodes/{nid}/disconnect", headers=h)
    c.post("/api/xray/stop", headers=h)
    for client_id in (lost, kept):                         # the config is left with no inbound
        assert c.delete(f"/api/rw/clients/{client_id}", headers=h).json()["revocation"] == "cleaned"
    added = c.post("/api/rw/clients", json={"email": "tablet"}, headers=h).json()
    body = c.delete(f"/api/rw/clients/{added['clients'][0]['id']}", headers=h).json()

    assert body["revocation"] == "not-live"
    assert body["revocation_pending"] is False
    assert c.get("/api/rw").json()["revocation_pending"] is False


class _MarkerUnreadable(Exception):
    pass


def test_a_marker_that_cannot_be_read_is_never_reported_as_false(settings, stub_xray, monkeypatch):
    from pi_gw_panel.controller import RW_PENDING_KEY

    c = _client(settings, stub_xray)
    _login(c)
    store = c.app.state.app_state.store
    real_get = store.get_setting

    def _get(key, *args, **kwargs):
        if key == RW_PENDING_KEY:
            raise _MarkerUnreadable("simulated settings read failure")
        return real_get(key, *args, **kwargs)

    monkeypatch.setattr(store, "get_setting", _get)
    with pytest.raises(_MarkerUnreadable):
        c.get("/api/rw")
