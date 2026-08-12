"""Boot when the panel cannot install the enforcement it renders: visibility is not enforcement.

`boot_guard` refuses to install a ruleset it cannot prove covers every interface the segment may be
carrying, and that refusal is correct — a ruleset is REPLACED, so a short one uncovers a live
interface. Its OUTCOME at boot was the defect. On a panel restart the refusal inherits the previous,
correctly-covering ruleset and costs nothing; on a HOST reboot it inherits no nft table at all, while
`host_provision` has just turned forwarding on. Forwarded client traffic then goes out direct.
`/api/ready` reported it red, and readiness is observability: no packet consults it.

So the refusal is no longer discarded. Underneath the interface-scoped enforcement there is a second
kind for exactly the state in which the first cannot be rendered — a FORWARD deny that names no
interface, which is the only shape available when the unknown is *which interfaces to name*. It:

  * cannot lock the operator out, and by construction rather than by a carve-out: it is registered
    on the `forward` hook, which only transit packets traverse. Traffic addressed to this machine
    (the panel, SSH, the segment's own DHCP/DNS) goes prerouting -> input, and traffic the host
    originates goes output -> postrouting. Neither is reachable from a forward chain;
  * lives in its own nft table, so the apply that may not have covered everything cannot remove it
    and it can never be mistaken for the enforcement it stands in for;
  * is recoverable: the first later pass that can render a complete cover installs the real
    enforcement and deletes it.

And boot no longer proceeds as though enforced. With the deny proven in force the gateway is closed
harder than configured, so the traffic machinery may start — while `/api/ready` says which of the
three states `enforcement: false` means. With the deny REFUSED nothing is known to be holding the
forward path, and the tunnel reapply and the unattended loops do not start at all; the panel still
serves the management API, because it is the screen the operator would fix the gateway from.
"""
import subprocess

import pytest
from conftest import _login
from fastapi.testclient import TestClient

from pi_gw_panel.app import create_app
from pi_gw_panel.net_control import provision
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.net_control.plan import NetResult
from pi_gw_panel.net_control.provision import EMERGENCY_TABLE
from pi_gw_panel.state import build_state

FAMILIES = {("ip", EMERGENCY_TABLE), ("ip6", EMERGENCY_TABLE)}


class _Host(DryRunBackend):
    """The dry-run backend plus the `_run` seam, which is what makes every path take its host branch.

    It models the one host fact these tests are about: which nft tables are loaded. `nft -f` loads
    the tables its script declares, `nft list table` answers for one, and `nft delete table` removes
    it — each answering the way iproute2/nft do for something that is not there, because "no such
    table" is the answer the release path reads as success.
    """

    def __init__(self, nft_refuses=False, links=("eth0", "eth0.2")):
        super().__init__()
        self.nft_refuses = nft_refuses
        self.links = set(links)
        self.tables: set[tuple[str, str]] = set()
        self.scripts: list[str] = []
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
            if self.nft_refuses:
                raise subprocess.CalledProcessError(
                    1, cmd, stderr="Error: Could not process rule: Operation not permitted")
            self.scripts.append(script or "")
            for line in (script or "").splitlines():
                parts = line.split()
                if parts[:1] == ["table"] and len(parts) >= 3:
                    self.tables.add((parts[1], parts[2]))
        elif cmd[1:3] == ["list", "table"]:
            if (cmd[3], cmd[4]) not in self.tables:
                raise subprocess.CalledProcessError(1, cmd, stderr=absent)
        elif cmd[1:3] == ["delete", "table"]:
            if (cmd[3], cmd[4]) not in self.tables:
                raise subprocess.CalledProcessError(1, cmd, stderr=absent)
            self.tables.discard((cmd[3], cmd[4]))
        return subprocess.CompletedProcess(cmd, 0, "", "")


class _Component:
    """A background component that records whether boot started it."""

    def __init__(self, name, started):
        self.name, self._started = name, started

    def start(self):
        self._started.append(self.name)

    async def stop(self):
        pass


def _unreadable(state, monkeypatch, key=provision.SURVIVOR_KEY):
    """A store that will not answer for ONE of the cover's records — a transient fault, not a
    broken box. It is what makes `boot_guard`'s render refuse."""
    original = state.store.get_setting

    def reading(name):
        if name == key:
            raise RuntimeError("simulated settings read failure")
        return original(name)

    monkeypatch.setattr(state.store, "get_setting", reading)
    return lambda: monkeypatch.setattr(state.store, "get_setting", original)


def _booted(settings, stub_xray, monkeypatch, net, unreadable=True):
    """Bring the real app up through its real lifespan. Returns `(client, state, started, reapplied)`.

    The background components are stubs and `reapply_active_node` is recorded rather than run: the
    question every test below asks is whether boot went on to drive traffic, and those are the two
    places it would.
    """
    settings.xray_bin = stub_xray
    state = build_state(settings, net=net)
    state.dnsmasq = state.pd_client = None
    started: list[str] = []
    reapplied: list[bool] = []
    state.recorder = _Component("recorder", started)
    monkeypatch.setattr("pi_gw_panel.app.SubScheduler",
                        lambda _s: _Component("scheduler", started))
    monkeypatch.setattr("pi_gw_panel.app.HealthMonitor",
                        lambda _s: _Component("monitor", started))
    monkeypatch.setattr("pi_gw_panel.app.LivenessLoop",
                        lambda _s: _Component("liveness", started))
    monkeypatch.setattr("pi_gw_panel.backup.scheduler.BackupScheduler",
                        lambda _s: _Component("backup", started))
    monkeypatch.setattr("pi_gw_panel.controller.reapply_active_node",
                        lambda _s: reapplied.append(True))
    restore = _unreadable(state, monkeypatch) if unreadable else (lambda: None)
    return create_app(settings, state=state), state, started, reapplied, restore


# --- the fallback's own shape --------------------------------------------------------------------


def test_the_emergency_ruleset_names_no_interface_and_hooks_only_forward():
    """Both non-negotiables of the fallback, read off the script it loads.

    Interface-independent BY CONSTRUCTION: the reason the normal render refused is that the panel
    cannot say which interfaces to name, so a fallback containing a name would be refusable for the
    same reason. And forward-only, which is why it cannot lock the operator out — an `input` or
    `output` chain is the only way a table here could touch traffic to or from this host.
    """
    script = provision._EMERGENCY_SCRIPT

    assert "hook forward" in script
    assert "policy drop" in script
    assert "hook input" not in script and "hook output" not in script
    assert "iifname" not in script and "oifname" not in script and "eth" not in script
    for family in ("ip", "ip6"):                 # both families, or v6 forwards around the deny
        assert f"table {family} {EMERGENCY_TABLE}" in script
    assert EMERGENCY_TABLE != "pi_gw_panel", "the normal apply deletes and recreates its own table"


def test_a_confirmed_enforcement_removes_a_deny_a_previous_process_left_behind(settings, stub_xray):
    """The marker is in-memory and the table is in the kernel, so the release cannot be gated on it:
    a process that installed the deny and was restarted would otherwise drop every forwarded packet
    for ever, with nothing recording why."""
    settings.xray_bin = stub_xray
    net = _Host()
    net.tables |= FAMILIES                       # the leftover, with nothing in this process's memory
    state = build_state(settings, net=net)
    state.dnsmasq = state.pd_client = None
    try:
        assert provision.enforcement_fallback_note(net) == ""

        assert provision.enforcement_fallback(state, NetResult(ok=True)) == ""

        assert net.tables & FAMILIES == set()
    finally:
        state.close()


# --- boot, with the cover unprovable ------------------------------------------------------------


def test_boot_holds_the_forward_path_closed_when_the_guard_is_refused(settings, stub_xray,
                                                                     monkeypatch):
    """PRODUCT-CRITICAL. The reproduction: boot renders the leak-guard, cannot prove the cover, and
    installs nothing — which on a host reboot means no table at all over forwarding that is on."""
    app, state, _started, _reapplied, _restore = _booted(
        settings, stub_xray, monkeypatch, _Host())
    with TestClient(app) as client:
        assert state.net.tables & FAMILIES == FAMILIES, \
            "boot left the forward path with nothing on it"
        assert "policy drop" in state.net.scripts[-1]
        # ...and the state is REPORTED, not merely logged: which of three things `false` means.
        payload = client.get("/api/ready").json()
        assert payload["checks"]["enforcement"] is False
        assert "emergency deny" in payload["details"]["enforcement"]
        assert "simulated settings read failure" in payload["details"]["enforcement"]


def test_the_operator_can_still_reach_the_panel_while_the_deny_is_in_force(settings, stub_xray,
                                                                          monkeypatch):
    """The management path is this machine's own address, so it is `input`, not `forward`. Nothing
    the fallback installs can touch it — and it must not, because this is the screen the gateway
    gets fixed from. Asserted end to end: the API answers, and a session can be established."""
    app, state, _started, _reapplied, _restore = _booted(
        settings, stub_xray, monkeypatch, _Host())
    with TestClient(app) as client:
        assert state.net.tables & FAMILIES == FAMILIES

        assert client.get("/api/health").json() == {"status": "ok"}
        token = _login(client)
        assert client.get("/api/nodes", headers={"X-CSRF-Token": token}).status_code == 200
        assert client.get("/api/ready").status_code == 503     # reachable AND honest


def test_boot_does_not_drive_traffic_when_nothing_can_be_shown_to_hold_the_path(settings, stub_xray,
                                                                               monkeypatch):
    """THE SEMANTIC CASE. The deny is refused too, so nothing is known to be holding the forward
    path — and "do not continue until fail-closed enforcement is confirmed" is what that means
    concretely: no tunnel reapply, and none of the unattended loops that restart xray and fail nodes
    over. Removing that hold while keeping the fallback is exactly the mutation this catches.

    Boot still comes UP: refusing to start would take away the management API, and an unreachable
    panel is not the safe end of an unenforceable gateway.
    """
    app, state, started, reapplied, _restore = _booted(
        settings, stub_xray, monkeypatch, _Host(nft_refuses=True))
    monkeypatch.setattr(provision, "_write_proc", lambda path, value: False)
    with TestClient(app) as client:
        assert state.net.tables == set()                       # the deny could not go on
        assert reapplied == [], "boot brought a tunnel up over a forward path nobody vouches for"
        assert started == [], "boot started the unattended loops over an unguarded forward path"

        assert client.get("/api/health").json() == {"status": "ok"}
        note = client.get("/api/ready").json()["details"]["enforcement"]
        assert "could not be installed" in note


def test_the_last_resort_turns_forwarding_off_when_the_table_will_not_load(settings, stub_xray):
    """The second interface-independent lever, and it is explicitly NOT a substitute: `ensure_sysctls`
    writes forwarding back on, which is why the hold above is what makes this safe rather than this."""
    settings.xray_bin = stub_xray
    state = build_state(settings, net=_Host(nft_refuses=True))
    state.dnsmasq = state.pd_client = None
    written: list[tuple[str, str]] = []
    try:
        held = provision.install_emergency_forward_deny(
            state, "the cover could not be established",
            write_proc=lambda path, value: written.append((path, value)) or True)

        assert held                                            # boot may not proceed through this
        assert written == [("/proc/sys/net/ipv4/ip_forward", "0"),
                           ("/proc/sys/net/ipv6/conf/all/forwarding", "0")]
        assert "turned off instead" in held
    finally:
        state.close()


# --- recovery -----------------------------------------------------------------------------------


def test_a_later_pass_that_proves_the_cover_replaces_the_deny_with_real_enforcement(
        settings, stub_xray, monkeypatch):
    """The deny names no interface, so nothing narrows it and nothing drains it: the only thing that
    can end it is a render that PROVES a complete cover. The provisioning pass is where that is
    asked, because it is what boot, `PUT /api/network` and a restore all run."""
    app, state, _started, _reapplied, restore = _booted(
        settings, stub_xray, monkeypatch, _Host())
    with TestClient(app) as client:
        assert state.net.tables & FAMILIES == FAMILIES
        note = provision.enforcement_fallback_note(state.net)
        assert "emergency deny" in note

        restore()                                   # the transient store fault clears
        result = provision.host_provision(state)

        assert result.ok is True
        assert state.net.tables & FAMILIES == set(), "the deny outlived the enforcement it stood in for"
        assert provision.enforcement_fallback_note(state.net) == ""
        assert 'iifname "eth0.2"' in state.net.applied[-1]     # the panel's own scoped enforcement
        assert client.get("/api/ready").json()["details"].get("enforcement") is None


def test_a_pass_that_still_cannot_prove_the_cover_leaves_the_deny_and_says_so(settings, stub_xray,
                                                                             monkeypatch):
    """The other branch of the handover: the store is still broken, so the render refuses again, the
    deny stays exactly where it is, and the pass reports rather than calling itself applied."""
    app, state, _started, _reapplied, _restore = _booted(
        settings, stub_xray, monkeypatch, _Host())
    with TestClient(app):
        assert state.net.tables & FAMILIES == FAMILIES

        result = provision.host_provision(state)

        assert result.ok is False
        assert "still being dropped by the emergency deny" in result.error
        assert state.net.tables & FAMILIES == FAMILIES
        assert provision.enforcement_fallback_note(state.net)


# --- the ordinary boot is untouched --------------------------------------------------------------


@pytest.mark.parametrize("kill_switch", ["1", "0"])
def test_a_boot_that_can_enforce_installs_no_emergency_deny(settings, stub_xray, monkeypatch,
                                                            kill_switch):
    """The other half of the contract, for both configured intents. With the kill-switch on the guard
    goes on and the fallback has nothing to do; with it off a teardown IS the configured intent —
    clients falling back to direct is what the operator asked for, not an accident — so there is
    nothing to hold closed either."""
    app, state, started, reapplied, _restore = _booted(
        settings, stub_xray, monkeypatch, _Host(), unreadable=False)
    state.store.set_setting("kill_switch_enabled", kill_switch)
    with TestClient(app):
        assert state.net.tables & FAMILIES == set()
        assert provision.enforcement_fallback_note(state.net) == ""
        assert reapplied == [True]
        assert started == ["scheduler", "monitor", "liveness", "backup", "recorder"]
