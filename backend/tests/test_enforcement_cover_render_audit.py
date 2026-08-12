"""The render side of the durable enforcement cover: what happens when it cannot be completed.

`enforcement_cover` answers with two facts — the interfaces that may still be carrying the segment,
and whether that is the WHOLE answer — and the provisioning pass has always honoured both: it will
not stage a transitional ruleset, and will not narrow one, on an incomplete answer. The RENDER path
only logged it and applied the names it had. Since a ruleset is replaced rather than edited, that
turned one unreadable record into the exact bypass the protocol exists to prevent: a `sync_net` with
no pass running anywhere replaced a ruleset covering `{eth0.2, eth0.9}` with one covering `{eth0.2}`,
while `eth0.9` was up carrying the segment address a rolled-back change had put there.

So the render refuses. Nothing is applied, the ruleset already on the host stays exactly as it is,
and the caller is told. These tests pin that for every caller of the two store-derived render
functions, and pin that a COMPLETE cover still renders precisely what it did before.
"""
import re
import subprocess

import pytest
from conftest import _login
from fastapi.testclient import TestClient

from pi_gw_panel import backup as backup_mod
from pi_gw_panel import controller
from pi_gw_panel.app import create_app
from pi_gw_panel.controller import (
    EnforcementCoverUnknown,
    apply_net,
    apply_node,
    boot_guard,
    restore_backup,
    stop_net,
    sync_net,
)
from pi_gw_panel.health import failover
from pi_gw_panel.health.monitor import HealthMonitor
from pi_gw_panel.models import Node
from pi_gw_panel.net_control import provision
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.state import build_state

SURVIVOR = ("eth0.9", "192.168.10.2/24")


class _Net(DryRunBackend):
    """The dry-run backend with the `_run` seam that makes provisioning take its linux branch.

    Deliberately thin: these tests are about the ORDER the controller does things in and about which
    rulesets reach a backend, not about what the host does with them. Every command succeeds; a
    `show` for a device that is not on the host answers the way iproute2 does, because that is what
    the candidate link probe reads.
    """

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


class _Refusing(DryRunBackend):
    """A backend that fails the test if a ruleset reaches it at all."""

    def apply_tproxy(self, plan):
        raise AssertionError(f"a tproxy ruleset was installed: {plan}")

    def apply_guard(self, plan):
        raise AssertionError(f"a guard ruleset was installed: {plan}")

    def teardown(self):
        raise AssertionError("the ruleset was torn down")


def _state(settings, stub_xray, net=None):
    settings.xray_bin = stub_xray
    state = build_state(settings, net=net if net is not None else DryRunBackend())
    state.dnsmasq = state.pd_client = None
    return state


def _node(store, name="n1", address="1.2.3.4") -> Node:
    return store.get_node(store.add_node(Node(
        id=None, name=name, address=address, port=47000, uuid=f"u-{name}",
        sni="www.microsoft.com", public_key="PK", short_id="ab12")))


def _enforced(text: str) -> set:
    """Every interface a rendered ruleset scopes a segment rule to — one name or a set of them."""
    found = set()
    for one, many in re.findall(r'iifname (?:"([^"]+)"|\{([^}]*)\})', text):
        found |= {one} if one else set(re.findall(r'"([^"]+)"', many))
    return found


def _survivor(state):
    """A rolled-back change left `eth0.9` up and carrying the segment address it put there — the
    state the durable cover exists for, and the one an unreadable record hides."""
    assert provision.remember_survivors(state.store, [SURVIVOR]) is True


def _connected(state, monkeypatch):
    """The two facts `_enforcement_mode` reads to call for a tproxy ruleset."""
    state.store.set_setting("active_node_id", "1")
    monkeypatch.setattr(state.supervisor, "status", lambda: {"running": True})


def _unreadable(state, monkeypatch, key=provision.SURVIVOR_KEY):
    """A store that will not answer for ONE of the cover's records, at render time, with no pass
    running. Every other read behaves — this is a transient fault, not a broken box."""
    original = state.store.get_setting

    def reading(name):
        if name == key:
            raise RuntimeError("simulated settings read failure")
        return original(name)

    monkeypatch.setattr(state.store, "get_setting", reading)


def _live_ruleset(state, monkeypatch) -> str:
    """Install the ruleset a COMPLETE cover renders, and hand back what went on the host. Every
    "was it replaced" assertion below is against this."""
    _survivor(state)
    _connected(state, monkeypatch)
    assert sync_net(state).ok is True
    installed = state.net.applied[-1]
    assert _enforced(installed) == {"eth0.2", "eth0.9"}, "the fixture did not cover both interfaces"
    return installed


def _unchanged(state, installed: str, count: int = 1):
    """The live ruleset is the one that was there: same number of applies, same last render.

    Both halves matter. A narrowing shows up as an extra apply whose render is short, and a render
    that is merely re-installed identically is not what "preserve" means either — the backend must
    not have been entered at all.
    """
    assert len(state.net.applied) == count, "a ruleset was installed on an incomplete cover"
    assert state.net.applied[-1] == installed
    assert _enforced(state.net.applied[-1]) == {"eth0.2", "eth0.9"}


# --- the render itself --------------------------------------------------------------------------


def test_an_incomplete_cover_yields_no_plan_at_all(settings, stub_xray, monkeypatch):
    """`_enforcement_plan` used to return the names that answered. There is no such plan: the names
    that answered describe a host only if the one that did not was naming nothing, which is exactly
    what cannot be known here."""
    state = _state(settings, stub_xray)
    _survivor(state)
    assert provision.enforcement_cover(state.store, "eth0.2").names == ["eth0.9"]
    _unreadable(state, monkeypatch)

    with pytest.raises(EnforcementCoverUnknown) as raised:
        controller._enforcement_plan(state.settings, state.store)

    assert "simulated settings read failure" in str(raised.value)
    state.close()


@pytest.mark.parametrize("call", ["apply_net", "stop_net"])
def test_neither_render_path_enters_the_backend_on_an_incomplete_cover(settings, stub_xray,
                                                                      monkeypatch, call):
    """The whole of "preserve the existing ruleset", for a backend that only ever replaces one:
    do not call it. A backend that raises on every entry point proves nothing was rendered onto the
    host — not a tproxy ruleset, not a guard, and not a teardown either."""
    state = _state(settings, stub_xray, _Refusing())
    _survivor(state)
    _unreadable(state, monkeypatch)

    result = (apply_net if call == "apply_net" else stop_net)(
        state.settings, state.net, state.store)

    assert result.ok is False
    assert "could not be established" in result.error
    assert state.net.enforcement_status == "error"
    assert state.net.wan_blocked is None                 # nothing was proven about client→WAN
    state.close()


def test_a_complete_cover_still_renders_exactly_what_it_did(settings, stub_xray, monkeypatch):
    """The other half of the contract: covering is unchanged. A cover with names is installed over
    all of them, an EMPTY cover that every source answered renders the plan's own interface alone,
    and the guard follows the same rule as the tproxy ruleset."""
    state = _state(settings, stub_xray)
    _connected(state, monkeypatch)

    assert sync_net(state).ok is True                             # nothing recorded: just eth0.2
    assert _enforced(state.net.applied[-1]) == {"eth0.2"}

    _survivor(state)
    assert sync_net(state).ok is True                             # a survivor: both
    assert _enforced(state.net.applied[-1]) == {"eth0.2", "eth0.9"}

    monkeypatch.setattr(state.supervisor, "status", lambda: {"running": False})
    assert sync_net(state).ok is True                             # ...and so does the guard
    assert _enforced(state.net.applied[-1]) == {"eth0.2", "eth0.9"}
    assert "tproxy" not in state.net.applied[-1]
    state.close()


# --- one caller at a time -----------------------------------------------------------------------


def test_sync_net_does_not_replace_a_live_tproxy_ruleset(settings, stub_xray, monkeypatch):
    """PRODUCT-CRITICAL, and the reproduction verbatim: no provisioning pass anywhere, a segment
    edit or any other sync, and the survivor record will not answer. This replaced a ruleset
    covering both interfaces with one covering the configured one — a direct-WAN window on `eth0.9`
    for as long as it stayed up."""
    state = _state(settings, stub_xray)
    installed = _live_ruleset(state, monkeypatch)
    _unreadable(state, monkeypatch)

    result = sync_net(state)

    assert result.ok is False
    assert "left exactly as it is" in result.error
    _unchanged(state, installed)
    state.close()


def test_sync_net_does_not_replace_a_live_ruleset_with_a_short_guard(settings, stub_xray,
                                                                    monkeypatch):
    """The other branch of the same caller. With the tunnel down the sync calls for the fail-closed
    guard, which is a replacement like any other: a short one takes the kill-switch drop off an
    interface that may be up carrying the segment."""
    state = _state(settings, stub_xray)
    installed = _live_ruleset(state, monkeypatch)
    monkeypatch.setattr(state.supervisor, "status", lambda: {"running": False})
    _unreadable(state, monkeypatch)

    result = sync_net(state)

    assert result.ok is False
    _unchanged(state, installed)
    state.close()


def test_a_teardown_is_not_gated_by_the_cover(settings, stub_xray, monkeypatch):
    """The one mode that is unaffected, and why. With the kill-switch OFF a stop tears the table
    down: it names no interface, so there is no coverage to be short of, and clients falling back
    to direct is the configured intent rather than an accident."""
    state = _state(settings, stub_xray)
    _live_ruleset(state, monkeypatch)
    state.store.set_setting("kill_switch_enabled", "0")
    monkeypatch.setattr(state.supervisor, "status", lambda: {"running": False})
    _unreadable(state, monkeypatch)

    assert sync_net(state).ok is True
    assert state.net.applied == []                       # the dry-run backend's teardown
    state.close()


def test_boot_guard_reports_instead_of_narrowing_what_the_kernel_still_holds(settings, stub_xray,
                                                                            monkeypatch):
    """Boot is not a clean slate — nft rules live in the kernel, so a panel or container restart
    arrives with the previous, correctly-covering ruleset still installed. A guard rendered from an
    incomplete cover would narrow exactly that, so boot gets a reported failure and the ruleset it
    inherited."""
    state = _state(settings, stub_xray)
    installed = _live_ruleset(state, monkeypatch)
    monkeypatch.setattr(state.supervisor, "status", lambda: {"running": False})
    _unreadable(state, monkeypatch)

    result = boot_guard(state)

    assert result.ok is False
    assert state.net.enforcement_status == "error"        # what boot carries into /api/health
    _unchanged(state, installed)
    state.close()


def test_apply_node_fails_closed_without_touching_the_ruleset(settings, stub_xray, monkeypatch):
    """A Connect (and every other caller of `apply_node`: boot reapply, a subscription refresh,
    the failover tick). The net step fails, so the config is rolled back and the recovery stops
    xray — and the guard it would install is refused for the same reason, which is reported. What
    the clients are left with is the previous ruleset pointing at a stopped xray: black-holed, not
    released to the WAN."""
    state = _state(settings, stub_xray)
    installed = _live_ruleset(state, monkeypatch)
    node = _node(state.store)
    monkeypatch.undo()                                   # the real supervisor from here on
    _survivor(state)
    _unreadable(state, monkeypatch)

    result = apply_node(node, state.settings, state.supervisor, state.net,
                        store=state.store, xray_bin=stub_xray)

    assert result.ok is False
    assert "network apply failed" in result.error
    assert "fail-closed recovery failed" in result.error
    assert state.supervisor.status()["running"] is False
    _unchanged(state, installed)
    state.close()


def test_the_failover_tick_keeps_the_active_node_and_the_ruleset(settings, stub_xray, monkeypatch):
    """The unattended caller. A failover that cannot install enforcement for the candidate must not
    complete: the switch fails, the store keeps naming the node it was told to flee, and the ruleset
    the previous apply installed is the one still on the host."""
    net = DryRunBackend()
    state = _state(settings, stub_xray, net)
    a, b = _node(state.store, "a", "1.1.1.1"), _node(state.store, "b", "2.2.2.2")
    assert apply_node(a, state.settings, state.supervisor, state.net,
                      store=state.store, xray_bin=stub_xray).ok is True
    try:
        _survivor(state)
        assert sync_net(state).ok is True
        installed = state.net.applied[-1]
        applies = len(state.net.applied)
        state.store.set_setting("health_hysteresis", "1")
        _unreadable(state, monkeypatch)
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

        monitor._tick()

        assert state.store.get_setting("active_node_id") == str(a.id), \
            "a failover completed without enforcement for the node it switched to"
        assert state.store.get_setting("last_failover_at") in (None, "")
        assert len(state.net.applied) == applies and state.net.applied[-1] == installed
        assert _enforced(state.net.applied[-1]) == {"eth0.2", "eth0.9"}
        assert b.id is not None
    finally:
        state.supervisor.stop()
        state.close()


def test_the_network_route_rolls_back_and_leaves_the_ruleset_alone(settings, stub_xray, monkeypatch):
    """`PUT /api/network`, whose rollback renders enforcement twice more. All three renders refuse,
    the edit is rolled back with the transaction, the operator gets a 502 naming the reason, and the
    ruleset that was on the host before the request is still the one on it."""
    state = _state(settings, stub_xray)
    installed = _live_ruleset(state, monkeypatch)
    client = TestClient(create_app(settings, state=state))
    headers = {"X-CSRF-Token": _login(client)}
    _unreadable(state, monkeypatch)

    response = client.put("/api/network", json={"segment_iface": "eth0.9"}, headers=headers)

    assert response.status_code == 502
    assert "could not be established" in response.json()["detail"]
    assert (state.store.get_setting("segment_iface") or "") in ("", "eth0.2"), \
        "the retarget was committed by a request that could not enforce it"
    _unchanged(state, installed)


def test_the_restore_rollback_leaves_the_ruleset_alone(settings, stub_xray, monkeypatch):
    """`restore_backup`, which renders enforcement for the disconnected state before it imports
    anything. It refuses, so nothing is imported and nothing is applied — the gateway is the one
    the operator had, with the ruleset it had."""
    state = _state(settings, stub_xray)
    installed = _live_ruleset(state, monkeypatch)
    document = backup_mod.export_state(state.store)
    monkeypatch.undo()                                   # the real supervisor: the restore stops it
    _unreadable(state, monkeypatch)

    result = restore_backup(state, document)

    assert result.ok is False
    assert "could not be established" in result.error
    _unchanged(state, installed)
    state.close()
