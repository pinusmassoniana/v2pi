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

from pi_gw_panel import controller
from pi_gw_panel.api import routes as routes_mod
from pi_gw_panel.app import create_app
from pi_gw_panel.net_control import provision
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.net_control.plan import NetResult
from pi_gw_panel.net_control.provision import EMERGENCY_TABLE
from pi_gw_panel.state import build_state

FAMILIES = {("ip", EMERGENCY_TABLE), ("ip6", EMERGENCY_TABLE)}

# The step that opens the forward path, as it appears in the traced boot sequence below.
FORWARDING_ON = "ip_forward=1"


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


def _sequence(monkeypatch, steps):
    """Record the boot steps whose ORDER is the finding, each one still calling through.

    Wrapping rather than replacing, because the question is not whether these run in isolation but
    what runs before what: the leak-guard used to be resolved AFTER `host_provision`, whose first act
    is `ensure_sysctls` — forwarding on — so the window the guard exists to close was open for the
    length of a provisioning pass. `ip_forward=1` is therefore recorded in the same list as the
    functions, and the assertions below read one sequence.

    The `/proc` writer is the one thing NOT called through: bound to the real one, a suite running as
    root would turn the host's own forwarding on while testing that boot does not.
    """
    def trace(module, name):
        original = getattr(module, name)

        def traced(*args, **kwargs):
            steps.append(name)
            return original(*args, **kwargs)

        monkeypatch.setattr(module, name, traced)

    trace(controller, "boot_guard")
    trace(provision, "resume_pending_provision_undo")
    trace(provision, "host_provision")
    trace(routes_mod, "rw_reconcile_pending")

    def writing(path, value):
        if path.endswith("ip_forward"):
            steps.append(f"ip_forward={value}")
        return True

    monkeypatch.setattr(provision, "_write_proc", writing)


def _booted(settings, stub_xray, monkeypatch, net, unreadable=True, steps=None):
    """Bring the real app up through its real lifespan. Returns `(client, state, started, reapplied)`.

    The background components are stubs and `reapply_active_node` is recorded rather than run: the
    question every test below asks is whether boot went on to drive traffic, and those are the two
    places it would. Pass `steps` to also trace the boot sequence itself (see `_sequence`).
    """
    settings.xray_bin = stub_xray
    if steps is not None:
        _sequence(monkeypatch, steps)
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


# --- the ORDER in which boot resolves it ---------------------------------------------------------
#
# The fallback above only holds the window shut if it is decided BEFORE the window opens. It was not:
# `host_provision` ran first, and `ensure_sysctls` — its first act — turns IPv4 and IPv6 forwarding
# on. So a host reboot spent an entire provisioning pass with forwarding enabled and no nft table at
# all, and the tier check that followed could not retroactively close it. Worse, the check was
# downstream of the machinery it gates: with the deny REFUSED, the host had still been provisioned
# and the segment interface raised, over a forward path nothing was accounting for.


def test_nothing_turns_forwarding_on_before_the_guard_and_its_fallback_have_answered(
        settings, stub_xray, monkeypatch):
    """PRODUCT-CRITICAL, and it is an ordering assertion because the defect was an ordering.

    The guard is refused here and the deny is PROVEN, which is the state boot may carry on through —
    the gateway is closed harder than configured. So the normal sequence proceeds, and the whole point
    is where `ip_forward=1` sits in it: after the forward path has been accounted for, not before.
    """
    steps: list[str] = []
    app, state, started, reapplied, _restore = _booted(
        settings, stub_xray, monkeypatch, _Host(), steps=steps)
    with TestClient(app):
        assert steps == ["boot_guard", "resume_pending_provision_undo", "host_provision",
                         FORWARDING_ON, "rw_reconcile_pending"]
        assert state.net.tables & FAMILIES == FAMILIES     # ...and it is the deny holding it
        assert reapplied == [True]
        assert started == ["scheduler", "monitor", "liveness", "backup", "recorder"]


def test_a_refused_deny_holds_back_everything_that_could_open_the_forward_path(settings, stub_xray,
                                                                              monkeypatch):
    """THE SEMANTIC CASE. Nothing can be shown to be holding the forward path, so nothing that could
    open or use it runs: forwarding is never turned on, the host is not provisioned, the interrupted
    undo is not resumed, the revocation is not reconciled, no tunnel is reapplied and no unattended
    loop starts. Keeping the reordering but letting provisioning run past `unguarded` is exactly the
    mutation the `FORWARDING_ON` assertion catches.

    And the panel still comes up: refusing to serve would take away the screen the gateway gets fixed
    from, which is never the safe end of an unenforceable gateway.
    """
    steps: list[str] = []
    app, state, started, reapplied, _restore = _booted(
        settings, stub_xray, monkeypatch, _Host(nft_refuses=True), steps=steps)
    with TestClient(app) as client:
        assert FORWARDING_ON not in steps, "boot turned forwarding on with nothing holding the path"
        assert steps == ["boot_guard", "ip_forward=0"], \
            "boot ran past the point where nothing could be shown to be holding the forward path"
        assert state.net.tables == set()                   # the deny could not go on either
        assert [cmd for cmd in state.net.cmds if cmd[:1] == ["ip"]] == [], \
            "boot reconfigured the host over a forward path nobody vouches for"
        assert reapplied == [], "boot brought a tunnel up over a forward path nobody vouches for"
        assert started == [], "boot started the unattended loops over an unguarded forward path"

        assert client.get("/api/health").json() == {"status": "ok"}
        token = _login(client)
        assert client.get("/api/nodes", headers={"X-CSRF-Token": token}).status_code == 200
        assert client.get("/api/ready").status_code == 503          # reachable AND honest


def test_a_first_ever_boot_with_nothing_configured_comes_up_guarded(settings, stub_xray, monkeypatch):
    """Moving the guard above provisioning must not make it depend on provisioning having happened.

    It does not, and by construction. The guard is rendered from the STORE — `NetPlan.from_store`
    resolves each editable field as store-override-or-config, so an empty store is the configured
    default rather than a missing answer — and it NAMES its interface instead of probing for it: nft
    matches `iifname` by name, so the segment VLAN this boot has not created yet is named for free,
    and `apply_guard` touches no link and no address. Every cover record is absent, which is an
    ANSWERED empty cover, so the guard installs, no fallback is needed, and the pass below then
    creates the interface the ruleset was already covering.
    """
    steps: list[str] = []
    app, state, started, reapplied, _restore = _booted(
        settings, stub_xray, monkeypatch, _Host(links=("eth0",)), unreadable=False, steps=steps)
    assert state.store.get_setting("managed_segment_iface") in (None, "")   # never provisioned
    with TestClient(app) as client:
        assert steps == ["boot_guard", "resume_pending_provision_undo", "host_provision",
                         FORWARDING_ON, "rw_reconcile_pending"]
        assert state.net.tables & FAMILIES == set(), "a first boot needed the emergency fallback"
        assert provision.enforcement_fallback_note(state.net) == ""
        assert 'iifname "eth0.2"' in state.net.applied[0], \
            "the first ruleset of the boot was not the guard over the configured segment"
        assert "eth0.2" in state.net.links          # ...and the pass then created what it covered
        assert client.get("/api/health").json() == {"status": "ok"}
        assert reapplied == [True]
        assert started == ["scheduler", "monitor", "liveness", "backup", "recorder"]


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
