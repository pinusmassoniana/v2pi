"""A failed recovery may not end with the ruleset down and the tunnel still up.

The "apply failed -> restore what we can -> install the fail-closed guard" sequence is open-coded
at several sites, and two of them could finish in that state:

  * `POST /rollback`, net branch — the reload SUCCEEDED, so xray is up on the rolled-back config,
    and then `apply_net` failed. The branch carried its sibling's message without its `stop()`.
  * `PUT /api/network` — a failed `sync_net` restore was only APPENDED to the recovery notes: the
    one site that reported its failure with no fail-closed attempt at all.

Both go through `_bounded_stop`, and both are asserted on the END STATE — what is running and what
rules are installed — rather than on the message that describes it.
"""
from conftest import _build_dryrun_state, _login
from fastapi.testclient import TestClient

from pi_gw_panel.app import create_app
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.net_control.plan import NetResult
from pi_gw_panel.state import build_state


def _net_client(settings, stub_xray):
    state = _build_dryrun_state(settings, stub_xray)
    c = TestClient(create_app(settings, state=state))
    return c, {"X-CSRF-Token": _login(c)}, state


class _Stoppable:
    """A live child that goes down on the first signal — the ordinary case, so that "xray is not
    left running" is an observable fact rather than an unkillable-process artefact."""

    pid = 5151

    def __init__(self):
        self.returncode = None
        self.signals: list[str] = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.signals.append("term")
        self.returncode = 0

    def kill(self):
        self.signals.append("kill")
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def _rollback_client(settings, stub_xray, monkeypatch, net=None):
    """A logged-in client whose rollback has a promotable candidate granting no remote access."""
    monkeypatch.setattr("pi_gw_panel.api.routes.ConfigManager.rollback_target",
                        lambda _self, *, log=False: {"inbounds": [], "outbounds": []})
    monkeypatch.setattr("pi_gw_panel.api.routes.ConfigManager.rollback", lambda _self: True)
    settings.xray_bin = stub_xray
    state = build_state(settings, net=net or DryRunBackend())
    c = TestClient(create_app(settings, state=state))
    return c, {"X-CSRF-Token": _login(c)}, state


def test_a_rollback_whose_net_apply_fails_does_not_leave_xray_running(settings, stub_xray,
                                                                     monkeypatch):
    """The rollback's net branch. The reload SUCCEEDED — xray is up on the rolled-back config —
    and then `apply_net` failed, so the rules are taken to their tunnel-down state. This branch
    carried its sibling's message without its `stop()`, ending exactly where no branch may: ruleset
    down, tunnel up."""
    c, h, state = _rollback_client(settings, stub_xray, monkeypatch)
    state.store.set_setting("prev_active_node_id", "1")
    child = _Stoppable()
    state.supervisor._proc = child
    monkeypatch.setattr(state.supervisor, "reload", lambda: True)
    monkeypatch.setattr(state.net, "apply_tproxy",
                        lambda plan: NetResult(ok=False, error="nft: apply denied"))

    r = c.post("/api/rollback", headers=h)

    assert r.status_code == 502
    assert state.supervisor.status()["running"] is False, \
        "the rules were taken down and xray was left running on the rolled-back config"
    assert "term" in child.signals
    guard = state.net.applied[-1]
    assert "chain forward" in guard and " drop" in guard and "tproxy ip to" not in guard, \
        "the fail-closed guard was not installed"


def test_put_network_asserts_the_guard_when_its_restore_fails(settings, stub_xray):
    """`PUT /api/network`, the only recovery site that reported its failure with no guard attempt
    at all. A `sync_net` that fails may have got part-way through the tproxy ruleset, so nothing is
    proven fail-closed until the guard is asserted."""
    class _ApplyFails(DryRunBackend):
        def apply_tproxy(self, plan):
            return NetResult(ok=False, error="nft: apply denied")

    settings.xray_bin = stub_xray
    state = build_state(settings, net=_ApplyFails())
    c = TestClient(create_app(settings, state=state))
    h = {"X-CSRF-Token": _login(c)}
    state.store.set_setting("active_node_id", "7")
    state.supervisor._proc = _Stoppable()          # a live tunnel: sync_net will take apply_net

    r = c.put("/api/network", json={"dhcp_end": "192.168.10.250"}, headers=h)

    assert r.status_code == 502
    guard = state.net.applied[-1]
    assert "chain forward" in guard and " drop" in guard and "tproxy ip to" not in guard, \
        "the failed restore left client to WAN in whatever shape the failed apply left it"
    assert state.supervisor.status()["running"] is True, \
        "the guard was installed, so the legitimate tunnel had no reason to be taken down"
    assert state.store.get_setting("dhcp_end") in (None, ""), "the intent did not roll back"


def test_put_network_stops_xray_when_even_the_guard_cannot_be_installed(settings, stub_xray):
    """...and when the guard itself cannot be installed, the tunnel does not stay up on rules that
    were never proven down."""
    class _EverythingFails(DryRunBackend):
        def apply_tproxy(self, plan):
            return NetResult(ok=False, error="nft: apply denied")

        def apply_guard(self, plan):
            return NetResult(ok=False, error="nft: guard denied")

    settings.xray_bin = stub_xray
    state = build_state(settings, net=_EverythingFails())
    c = TestClient(create_app(settings, state=state))
    h = {"X-CSRF-Token": _login(c)}
    state.store.set_setting("active_node_id", "7")
    child = _Stoppable()
    state.supervisor._proc = child

    r = c.put("/api/network", json={"dhcp_end": "192.168.10.250"}, headers=h)

    assert r.status_code == 502
    assert state.supervisor.status()["running"] is False, \
        "no guard could be installed and the tunnel was left up anyway"
    assert "term" in child.signals


def test_a_refused_config_is_not_a_recovery(settings, stub_xray):
    """A cross-field refusal happens before the transaction opens, so it has no recovery half at
    all: no host command, no rollback, no stopped tunnel."""
    c, h, state = _net_client(settings, stub_xray)
    state.store.set_setting("active_node_id", "7")
    state.supervisor._proc = _Stoppable()
    applied_before = list(state.net.applied)

    assert c.put("/api/network", json={"segment_iface": settings.mgmt_iface},
                 headers=h).status_code == 422
    assert state.net.applied == applied_before
    assert state.supervisor.status()["running"] is True, "a refusal took the live tunnel down"
