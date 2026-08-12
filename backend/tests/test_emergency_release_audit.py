"""Ending the emergency deny: the release belongs to every confirmed enforcement, not to one pass.

The interface-independent forward deny (`pi_gw_panel_emergency`) is installed when the panel cannot
prove which interfaces the segment may be on, and it names none itself — so nothing narrows it and
nothing drains it. Its release used to live only in the tail of `provision.host_provision`, which is
run by boot, `PUT /api/network` and a restore. Every OTHER recovery installs correct enforcement
without running a provisioning pass: a failover tick, a boot reapply, a subscription refresh, a
disconnect. Each of those left the deny in force — every forwarded packet dropped, indefinitely,
with nothing that would ever come back to it — while recording `enforcement_status="ok"`, so the
operator was told the gateway was fine while it carried nothing at all.

So the release moved to the ONE place a confirmed host enforcement is recognised:
`controller._record_enforcement`, which every apply, guard and teardown reaches. The tests below pin
the four properties that make that worth having:

  * a successful non-provisioning recovery ends the deny (the reproduction);
  * a release that did not take is a FAILURE, not a footnote — it goes back through the channel
    every caller already handles, and the gateway is not described as healthy;
  * `enforcement_status` (and so `/api/ready`'s `enforcement` check) cannot say "ok" while anything
    is recorded as holding the forward path;
  * an enforcement that was refused or failed does NOT release it — that is the hole it plugs.
"""
import subprocess

import pytest
from fastapi.testclient import TestClient

from pi_gw_panel import controller
from pi_gw_panel.app import create_app
from pi_gw_panel.health import failover
from pi_gw_panel.health.monitor import HealthMonitor
from pi_gw_panel.models import Node
from pi_gw_panel.net_control import provision
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.net_control.plan import NetResult
from pi_gw_panel.net_control.provision import EMERGENCY_TABLE
from pi_gw_panel.state import build_state

FAMILIES = {("ip", EMERGENCY_TABLE), ("ip6", EMERGENCY_TABLE)}


class _Host(DryRunBackend):
    """The dry-run backend plus the `_run` seam, which is what makes the emergency paths take their
    host branch. It models the one host fact these tests are about: which nft tables are loaded.

    `nft delete table` answers the way nft does for a table that is not there ("No such file or
    directory"), because that is the answer the release reads as success — the marker is in this
    process and the table is in the kernel, so the delete is attempted on every confirmation.
    `delete_refuses` is the host that will not let go of it.
    """

    def __init__(self, delete_refuses=False, links=("eth0", "eth0.2")):
        super().__init__()
        self.delete_refuses = delete_refuses
        self.links = set(links)
        self.tables: set[tuple[str, str]] = set()
        self.cmds: list[list[str]] = []

    def _run(self, cmd, input=None, **kw):
        self.cmds.append(list(cmd))
        if cmd[:1] == ["nft"]:
            return self._nft(cmd, input)
        rest = [tok for tok in cmd[1:] if tok not in ("-4", "-6", "-o")]
        if rest[:2] in (["link", "show"], ["addr", "show"]) and rest[-1] not in self.links:
            raise subprocess.CalledProcessError(
                1, cmd, stderr=f'Device "{rest[-1]}" does not exist.')
        if rest[:2] == ["link", "add"]:
            self.links.add(cmd[cmd.index("name") + 1])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def _nft(self, cmd, script):
        absent = "Error: No such file or directory"
        if cmd[1:3] == ["-f", "-"]:
            for line in (script or "").splitlines():
                parts = line.split()
                if parts[:1] == ["table"] and len(parts) >= 3:
                    self.tables.add((parts[1], parts[2]))
        elif cmd[1:3] == ["list", "table"]:
            if (cmd[3], cmd[4]) not in self.tables:
                raise subprocess.CalledProcessError(1, cmd, stderr=absent)
        elif cmd[1:3] == ["delete", "table"]:
            if self.delete_refuses:
                raise subprocess.CalledProcessError(
                    1, cmd, stderr="Error: Could not process rule: Operation not permitted")
            if (cmd[3], cmd[4]) not in self.tables:
                raise subprocess.CalledProcessError(1, cmd, stderr=absent)
            self.tables.discard((cmd[3], cmd[4]))
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture
def host_state(settings, stub_xray, request):
    """A panel on a host that has the emergency deny in force, as a boot that could not prove the
    cover leaves it — and nothing else about that boot: every test here is about a recovery that runs
    NO provisioning pass. `indirect=[True]` gives the host that will not let the deny go."""
    settings.xray_bin = stub_xray
    state = build_state(settings, net=_Host(delete_refuses=getattr(request, "param", False)))
    state.dnsmasq = state.pd_client = None
    held = provision.install_emergency_forward_deny(state, "the cover could not be established")
    assert held == "", "the fixture's own deny did not go on"
    assert state.net.tables & FAMILIES == FAMILIES
    assert provision.enforcement_fallback_note(state.net)
    try:
        yield state
    finally:
        state.supervisor.stop()
        state.close()


def _node(store, name="a", address="1.1.1.1"):
    return store.add_node(Node(id=None, name=name, address=address, port=443, uuid=f"u{name}",
                               sni="s", public_key="PK", short_id="sid"))


# --- the reproduction ----------------------------------------------------------------------------


def test_a_failover_tick_releases_the_deny_without_any_provisioning_pass(host_state):
    """PRODUCT-CRITICAL, and the regression this exists for. A failover tick reaches `apply_node`
    and nothing else — no boot, no `PUT /api/network`, no restore, so `host_provision` never runs and
    the handover that used to live in its tail never happens. The tick installed the panel's own
    enforcement over a deny that stayed in force: all forwarded traffic dropped, for ever, reported
    as a healthy gateway."""
    state, store = host_state, host_state.store
    a, b = _node(store, "a"), _node(store, "b", "2.2.2.2")
    assert controller.apply_node(store.get_node(a), state.settings, state.supervisor, state.net,
                                 store=store, xray_bin=state.xray_bin).ok
    store.set_setting("health_hysteresis", "1")
    state.net.tables |= FAMILIES              # in force again, as a later refused render leaves it
    provision.install_emergency_forward_deny(state, "the cover could not be established")

    monitor = HealthMonitor(
        state,
        tcp_ping=lambda addr, port, allow_private=False: (True, 5),
        http_ping=lambda addr, port, sni, allow_private=False: (True, 6),
        real_request=lambda proxy, url: (False, None, None, None),
        now_iso=lambda: "2026-06-03T00:00:00Z",
        after_tick=lambda: failover.run(
            state, now=1000.0,
            real_through=lambda *_a, **_kw: (True, 8, "203.0.113.9", None)),
    )
    monitor._tick()                           # the active node's real request fails → fail over

    assert store.get_setting("active_node_id") == str(b), \
        "the tick did not fail over, so it proves nothing about the release"
    assert state.net.tables & FAMILIES == set(), \
        "the recovered gateway is still dropping every forwarded packet"
    assert provision.enforcement_fallback_note(state.net) == ""
    assert state.net.enforcement_status == "ok"


def test_a_reapply_releases_the_deny(host_state):
    """The other non-provisioning recovery, and the one a restart takes: `reapply_active_node`
    re-establishes the saved node through the same funnel and runs no pass either."""
    state = host_state
    state.store.set_setting("active_node_id", str(_node(state.store)))

    result = controller.reapply_active_node(state)

    assert result is not None and result.ok, getattr(result, "error", "")
    assert state.net.tables & FAMILIES == set()
    assert provision.enforcement_fallback_note(state.net) == ""


@pytest.mark.parametrize("kill_switch", ["1", "0"])
def test_a_confirmed_stop_releases_the_deny_for_both_configured_intents(host_state, kill_switch):
    """A disconnect is a confirmed enforcement too, in both of its shapes.

    With the kill-switch ON the guard IS the enforcement, and ending the deny hands the segment back
    to it. With it OFF the teardown names no interface — so there is no bypass to reopen by releasing,
    the hazard the deny plugs being a ruleset short of a live interface — and what a surviving deny
    would do instead is hold the segment at NO network against a configuration whose entire content
    is "let these clients out directly". The operator asked for direct; a bare forward drop that
    nothing narrows and nothing drains is not a safer version of that answer, it is a different one.
    """
    state = host_state
    state.store.set_setting("kill_switch_enabled", kill_switch)

    result = controller.stop_net(state.settings, state.net, state.store)

    assert result.ok, result.error
    if kill_switch == "1":
        assert len(state.net.applied) == 1, "the fail-closed guard never went on"
    else:
        assert state.net.applied == [], "the teardown left a ruleset behind"
    assert state.net.tables & FAMILIES == set()
    assert provision.enforcement_fallback_note(state.net) == ""


# --- a release that did not take ----------------------------------------------------------------


@pytest.mark.parametrize("host_state", [True], indirect=True)
def test_a_release_that_fails_is_reported_and_the_gateway_is_not_called_healthy(host_state):
    """The enforcement is on the host and the gateway still carries nothing: the deny is dropping
    every forwarded packet. Swallowing that is the reported-healthy blackhole this change exists to
    end, so it comes back through the channel every caller already handles."""
    state = host_state

    result = controller.apply_net(state.settings, state.net, state.store)

    assert result.ok is False, "a gateway dropping all forwarded traffic reported success"
    assert "emergency deny" in result.error and "could not be removed" in result.error
    assert state.net.enforcement_status == "error"
    assert "emergency deny" in state.net.enforcement_error
    assert state.net.wan_blocked is None
    assert state.net.applied, "the enforcement itself never reached the backend"
    assert state.net.tables & FAMILIES == FAMILIES    # still in force, and still recorded as such
    assert provision.enforcement_fallback_note(state.net)


@pytest.mark.parametrize("host_state", [True], indirect=True)
def test_an_apply_whose_release_fails_fails_closed_instead_of_reporting_a_live_tunnel(host_state):
    """`apply_node` treats it as what it is — an apply that did not reach the state it was asked for
    — so it takes the same fail-closed path as any other failed net step, and no caller is handed
    `ok` for a tunnel whose clients have no network."""
    state = host_state
    node = state.store.get_node(_node(state.store))

    result = controller.apply_node(node, state.settings, state.supervisor, state.net,
                                   store=state.store, xray_bin=state.xray_bin)

    assert result.ok is False
    assert "network apply failed" in result.error and "emergency deny" in result.error
    assert not state.store.get_setting("active_node_id"), "a blackholed apply became the active node"
    assert state.net.enforcement_status == "error"


@pytest.mark.parametrize("host_state", [True], indirect=True)
def test_readiness_cannot_say_the_enforcement_is_ok_while_the_deny_is_in_force(host_state,
                                                                              settings):
    """End to end, through the contract the host migration script commits on: `enforcement` IS the
    recorded status, so a deny nobody could remove makes it false and puts the reason in `details`.
    "ok" here is `/api/ready` telling the operator a blackholed gateway is ready."""
    state = host_state
    client = TestClient(create_app(settings, state=state))   # no lifespan: only what is reported

    assert controller.apply_net(state.settings, state.net, state.store).ok is False

    payload = client.get("/api/ready").json()
    assert payload["checks"]["enforcement"] is False
    assert "emergency deny" in payload["details"]["enforcement"]
    client.post("/api/setup", json={"username": "admin", "password": "changeme"})
    network = client.get("/api/network").json()["status"]
    assert network["enforcement_status"] == "error"
    assert "emergency deny" in network["enforcement_error"]


def test_the_status_may_not_say_ok_while_anything_is_recorded_as_holding_the_path(host_state,
                                                                                 monkeypatch):
    """The invariant does not rest on the release's return value. A release that reports success
    while the note still stands — a swallowed failure, an implementation that forgot to clear it —
    must not be able to produce a healthy-looking gateway either, so the note is consulted too."""
    monkeypatch.setattr(provision, "release_emergency_forward_deny", lambda _scope: "")

    result = controller.apply_net(host_state.settings, host_state.net, host_state.store)

    assert result.ok is False
    assert "emergency deny" in result.error
    assert host_state.net.enforcement_status == "error"


def test_a_release_that_raises_is_reported_rather_than_assumed_away(host_state, monkeypatch):
    """"We could not find out" is not "it is gone". An undropped deny is a silent blackhole, so an
    unexpected raise out of the release is reported like any other release that did not take."""
    def exploding(_scope):
        raise RuntimeError("nft went away")

    monkeypatch.setattr(provision, "release_emergency_forward_deny", exploding)

    result = controller.apply_net(host_state.settings, host_state.net, host_state.store)

    assert result.ok is False
    assert "nft went away" in result.error
    assert host_state.net.enforcement_status == "error"


# --- nothing short of confirmed ------------------------------------------------------------------


def test_a_refused_enforcement_does_not_release_the_deny(host_state, monkeypatch):
    """THE SEMANTIC CASE. The deny is in force precisely because the panel cannot say which
    interfaces may be carrying the segment; a render made from the sources that DID answer may not be
    installed, so `apply_net` refuses without entering the backend. Releasing on that outcome hands
    the forward path back to nothing at all — the direct-WAN bypass the deny was standing in for.
    """
    state = host_state
    original = state.store.get_setting

    def reading(name):
        if name == provision.SURVIVOR_KEY:
            raise RuntimeError("simulated settings read failure")
        return original(name)

    monkeypatch.setattr(state.store, "get_setting", reading)

    result = controller.apply_net(state.settings, state.net, state.store)

    assert result.ok is False
    assert "could not be established" in result.error
    assert state.net.tables & FAMILIES == FAMILIES, "a refused render released the deny"
    assert state.net.applied == [], "a refused render entered the backend"
    assert state.net.enforcement_status == "error"


def test_a_backend_that_fails_the_apply_does_not_release_the_deny(host_state, monkeypatch):
    """The other unproven outcome: the render was fine and the HOST would not take it. Nothing is
    known to be enforcing the segment, so the deny is exactly what has to stay."""
    state = host_state
    monkeypatch.setattr(state.net, "apply_tproxy",
                        lambda _plan: NetResult(ok=False, error="nft refused the ruleset"))

    result = controller.apply_net(state.settings, state.net, state.store)

    assert result.ok is False
    assert state.net.tables & FAMILIES == FAMILIES
    assert provision.enforcement_fallback_note(state.net)


def test_a_backend_that_cannot_guard_does_not_release_the_deny(host_state):
    """A stop with the kill-switch on and no `apply_guard` never enters a backend either: the
    refusal is recorded, and an intention to enforce releases nothing. The stub shares the real
    host's `_run`, so a release taken here would show up in the same table set."""
    state = host_state

    class _NoGuard:
        """The host seam and no fail-closed guard at all."""
        _run = staticmethod(state.net._run)
        apply_tproxy = staticmethod(lambda _plan: NetResult(ok=True))
        teardown = staticmethod(lambda: NetResult(ok=True))

    state.store.set_setting("kill_switch_enabled", "1")

    result = controller.stop_net(state.settings, _NoGuard(), state.store)

    assert result.ok is False
    assert "cannot install fail-closed guard" in result.error
    assert state.net.tables & FAMILIES == FAMILIES
