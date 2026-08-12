import ipaddress
import json
import re
import subprocess

import pytest

from pi_gw_panel.config import Settings, SETTINGS_DEFAULTS
from pi_gw_panel.controller import boot_guard, sync_net
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.net_control.plan import NetPlan, NetResult
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.net_control.linux import TIMEOUT_RETURNCODE
from pi_gw_panel.net_control.render import render_nft, render_nft6
from pi_gw_panel.net_control import pd_client, provision


def _store():
    conn = connect(":memory:")
    init_schema(conn)
    return NodeStore(conn)


class HostFacts:
    """The per-interface facts every runner fake below records, and none of them used to.

    `up` is the set of interfaces the host currently has UP; `on` maps an interface to the
    addresses that are on THAT interface. Both were invisible here: `ip link set … up` fell
    through every fake as an unrecorded no-op, and the addresses were one flat set with no
    interface attached to them. So "the candidate is up and carrying the new IPv4 while dnsmasq,
    the NM drop-in and the nft/tproxy policy all still name the interface it replaces" and "the
    candidate is down and carries nothing" produced byte-identical fakes, and an ordering defect
    between those two states could not be written down as a test. A fake that cannot represent a
    state cannot be asked about it — which is why this is part of the fix and not scaffolding.

    `on` IS THE HOST. The flat `addrs` view is derived from it (see the property), rather than
    kept alongside it: while the two were maintained in parallel, `ip addr show dev <iface>`
    answered from the flat one and so reported addresses that are on a DIFFERENT interface. That
    is the one answer that licenses forgetting an ownership record, so the fake could tell a
    probe an address had gone from the interface it asked about while the host still had it there
    — and could not express the opposite either. Derivation removes the disagreement by
    construction; nothing can seed one view without the other.

    Facts are recorded where the command SUCCEEDS, never where it is issued: a refused `ip addr
    del` leaves the address exactly where it was, and a refused `ip link delete` leaves the link
    up, and a fake that recorded intent would report the opposite of the host in exactly the cases
    these tests exist for.
    """

    def __init__(self, up=(), on=None):
        self.up = set(up)
        self.on = {iface: set(addrs) for iface, addrs in (on or {}).items()}
        self.calls = []

    def note_link(self, cmd) -> None:
        """Record what a link command that SUCCEEDED did to the up/down state."""
        if cmd[:3] == ["ip", "link", "add"]:
            self.up.discard(cmd[cmd.index("name") + 1])      # a VLAN is created DOWN
        elif cmd[:3] == ["ip", "link", "set"] and cmd[4:5] in (["up"], ["down"]):
            (self.up.add if cmd[4] == "up" else self.up.discard)(cmd[3])
        elif cmd[:3] == ["ip", "link", "delete"]:
            self.up.discard(cmd[3])
            self.on.pop(cmd[3], None)                        # the link took its addresses with it

    def note_addr(self, rest) -> None:
        """`rest` is `["addr", "replace"|"del", <cidr>, "dev", <iface>]`, flags already stripped."""
        iface, addr = rest[4], rest[2]
        if rest[1] == "del":
            self.on.get(iface, set()).discard(addr)
        else:
            self.on.setdefault(iface, set()).add(addr)

    def is_up(self, iface) -> bool:
        return iface in self.up

    def addrs_on(self, iface) -> set:
        return set(self.on.get(iface, ()))

    def seed(self, iface: str, addrs) -> None:
        """Put addresses on an interface without issuing a command (host state a test starts from)."""
        self.on.setdefault(iface, set()).update(addrs)

    @property
    def addrs(self) -> frozenset:
        """Every address anywhere on this host — the union of `on`, and never its own record.

        Frozen so that seeding through it fails loudly instead of mutating a temporary: this used
        to be the writable record, and a test that adds an address here and nowhere else would
        otherwise describe a host on which `ip addr show` cannot find it.
        """
        return frozenset(addr for addrs in self.on.values() for addr in addrs)

    def link_line(self, iface: str) -> str:
        """What `ip link show <iface>` prints for a link that IS on the host.

        The admin flag is what `up` means here, so a test that raised or lowered a link through
        the fake gets that answer back from the probe as well — the two halves of "is it up" can
        no longer disagree.
        """
        flags = "BROADCAST,MULTICAST,UP,LOWER_UP" if iface in self.up else "BROADCAST,MULTICAST"
        state = "UP" if iface in self.up else "DOWN"
        return f"3: {iface}: <{flags}> mtu 1500 qdisc noqueue state {state} mode DEFAULT\n"

    def addr_lines(self, iface: str, ipv6: bool) -> str:
        """What `ip -o addr show dev <iface>` prints — the addresses on THAT interface, only."""
        return "".join(
            f"2: {iface}    {'inet6' if ':' in a else 'inet'} {a} scope global\n"
            for a in sorted(self.addrs_on(iface)) if (":" in a) == ipv6)


class FakeRun(HostFacts):
    """Records every command, and models the one fact its commands establish: which links are up.

    `ip link set … up/down` moves an interface in and out of `up` and `ip link show` answers from
    it, so a probe that asks this host whether the segment is up gets the answer this host's own
    commands produced. Without that the runner answered every probe with silence, which is
    `LINK_UNKNOWN` — indistinguishable from a wedged netlink, and not what a working host says.
    """

    def __init__(self, **facts):
        super().__init__(**facts)
        self.calls = []                                 # (cmd, input) pairs

    def __call__(self, cmd, input=None):
        self.calls.append((cmd, input))
        if cmd[:1] == ["ip"]:
            norm = ["ip"] + _strip_flags(cmd)
            if norm[:3] == ["ip", "link", "show"]:
                return subprocess.CompletedProcess(cmd, 0, self.link_line(norm[3]), "")
            self.note_link(norm)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def cmds(self):
        return [c for c, _ in self.calls]


def _strip_flags(cmd) -> list[str]:
    """`cmd` without the family/one-line switches, so a match can be written once per command."""
    return [tok for tok in cmd[1:] if tok not in ("-4", "-6", "-o")]


class FakeHost(HostFacts):
    """A runner whose `ip link add/delete` actually mutate a set of links, so a test can ask
    what is left on the host rather than only what was commanded. `ip link show` answers from
    that same set, so a probe for a link's absence gets the host's real answer — and, for a link
    that is there, reports whether it is UP out of the same `up` set every other answer uses."""

    def __init__(self, existing=(), **facts):
        super().__init__(**facts)
        self.links = set(existing)

    def __call__(self, cmd, input=None):
        self.calls.append(cmd)
        norm = ["ip"] + _strip_flags(cmd)
        if norm[:3] == ["ip", "link", "add"]:
            self.links.add(norm[norm.index("name") + 1])
        elif norm[:3] == ["ip", "link", "delete"]:
            if norm[3] not in self.links:
                raise subprocess.CalledProcessError(1, cmd)
            self.links.discard(norm[3])
        elif norm[:3] == ["ip", "link", "show"]:
            if norm[3] not in self.links:
                raise subprocess.CalledProcessError(
                    1, cmd, output="", stderr=f'Device "{norm[3]}" does not exist.')
            return subprocess.CompletedProcess(cmd, 0, self.link_line(norm[3]), "")
        self.note_link(norm)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def exists(self, iface):
        return iface in self.links

    def cmds(self):
        return list(self.calls)


class RefusingHost(FakeHost):
    """A host whose `ip link delete` fails for a reason that is NOT "already gone" — the link is
    still up afterwards. `returncode=TIMEOUT_RETURNCODE` is how the bounded runner reports a
    delete that never came back, which says nothing at all about the link."""

    def __init__(self, existing=(), refuse=(), returncode=1,
                 stderr="RTNETLINK answers: Operation not permitted", **facts):
        super().__init__(existing, **facts)
        self.refuse, self.returncode, self.stderr = set(refuse), returncode, stderr

    def __call__(self, cmd, input=None):
        norm = ["ip"] + _strip_flags(cmd)
        if norm[:3] == ["ip", "link", "delete"] and norm[3] in self.refuse:
            self.calls.append(cmd)
            raise subprocess.CalledProcessError(self.returncode, cmd, output="",
                                                stderr=self.stderr)
        return super().__call__(cmd, input)


class AddrHost(HostFacts):
    """A runner that models the ADDRESSES on an interface, the way `FakeHost` models the links.

    `ip addr replace/del` move an address on or off the interface the command NAMES, `ip -o addr
    show dev <iface>` answers with what is on THAT interface, and a delete named in `refuse` fails
    while leaving the address exactly where it was — which is what EPERM and the runner's time
    limit both do, and the state the reconcile/clear paths have to survive.

    Seeding: `on={iface: {addr, …}}` says which interface each address is on; `addrs=[…]` is the
    shorthand for "on the segment" and puts them on `default_iface`. There is no third, flat
    record — `HostFacts.addrs` is derived — so a seeded address is always somewhere.

    A refusal may be keyed by CIDR (`"192.168.9.2/24"`, refused wherever it is issued) or by the
    PAIR (`("eth0.2", "192.168.9.2/24")`, refused only on that interface). The pair is what a
    retarget needs: the same address can be on the way off one interface and on the way onto
    another in a single pass, and a fake keyed by CIDR alone answers for both at once.
    """

    def __init__(self, addrs=(), refuse=(), returncode=1,
                 stderr="RTNETLINK answers: Operation not permitted",
                 default_iface="eth0.2", **facts):
        super().__init__(**facts)
        self.seed(default_iface, addrs)
        self.refuse = set(refuse)
        self.returncode, self.stderr = returncode, stderr

    def blocked(self, blocks, iface: str, addr: str) -> bool:
        """Whether `blocks` names this command: the address anywhere, or on this interface."""
        return addr in blocks or (iface, addr) in blocks

    def __call__(self, cmd, input=None):
        self.calls.append(cmd)
        rest = _strip_flags(cmd)
        if rest[:2] == ["addr", "del"]:
            if self.blocked(self.refuse, rest[4], rest[2]):
                raise subprocess.CalledProcessError(self.returncode, cmd, output="",
                                                    stderr=self.stderr)
            self.note_addr(rest)
        elif rest[:2] == ["addr", "replace"]:
            self.note_addr(rest)
        elif rest[:2] == ["addr", "show"]:
            return subprocess.CompletedProcess(
                cmd, 0, self.addr_lines(rest[-1], "-6" in cmd), "")
        elif rest[:2] == ["link", "show"]:
            return subprocess.CompletedProcess(cmd, 0, self.link_line(rest[2]), "")
        else:
            self.note_link(["ip"] + rest)   # `ip link set … up/down` reaches the host here
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def cmds(self):
        return list(self.calls)


class UnansweringHost(FakeHost):
    """A host that answers NEITHER half: `ip link delete` and `ip link show` both hit the runner's
    time limit, which is how a wedged netlink presents. `TIMEOUT_RETURNCODE` + the runner's own
    synthetic stderr, so this is the exact exception `provision`'s default seam sees in
    production — no `link_exists` is injected against this fake, deliberately."""

    def __init__(self, existing=(), unanswered=(), **facts):
        super().__init__(existing, **facts)
        self.unanswered = set(unanswered)

    def __call__(self, cmd, input=None):
        if cmd[:2] == ["ip", "link"] and cmd[2] in ("delete", "show") and cmd[3] in self.unanswered:
            self.calls.append(cmd)
            raise subprocess.CalledProcessError(
                TIMEOUT_RETURNCODE, cmd, output="",
                stderr=f"command timed out after 10s: {' '.join(cmd)}")
        return super().__call__(cmd, input)


def _plan_for(iface: str) -> NetPlan:
    plan = NetPlan.from_settings(Settings())
    plan.segment_iface = iface
    return plan


class LinuxBackend:
    """The `_run` seam that gates the provision path onto "this is a real host", plus the
    enforcement seam a segment move drives.

    `apply_tproxy`/`apply_guard` render the REAL ruleset and record the text, so a test asks what
    the panel actually scoped its rules to rather than trusting a flag: which interfaces the
    kill-switch drop and the tproxy redirect name, at each point in the move, is the whole
    question. Failure is injectable per call, because an enforcement apply that will not go on is
    what must stop the move before anything is raised.
    """

    def __init__(self, run, fail=None):
        self._run = run
        self.applied: list[str] = []
        self.fail = fail            # a reason string => every enforcement apply reports failure
        self.kill_in = None         # the process dies as the Nth further ruleset is being loaded

    def _record(self, text: str, tunnel_up: bool) -> NetResult:
        if self.kill_in is not None:
            self.kill_in -= 1
            if self.kill_in < 0:
                raise KeyboardInterrupt("killed while loading a ruleset")
        if self.fail:
            return NetResult(ok=False, rendered=text, error=self.fail)
        self.applied.append(text)
        return NetResult(ok=True, rendered=text)

    def apply_tproxy(self, plan):
        return self._record(render_nft(plan) + render_nft6(plan), True)

    def apply_guard(self, plan):
        return self._record(
            render_nft(plan, tunnel_up=False) + render_nft6(plan, tunnel_up=False), False)

    def teardown(self):
        self.applied.append("")
        return NetResult(ok=True)


class _Supervisor:
    def __init__(self, running=False):
        self.running = running

    def status(self):
        return {"running": self.running}


def _enforced_ifaces(text: str) -> set:
    """Every interface a rendered ruleset scopes a segment rule to — one name or a set of them."""
    found = set()
    for one, many in re.findall(r'iifname (?:"([^"]+)"|\{([^}]*)\})', text):
        found |= {one} if one else set(re.findall(r'"([^"]+)"', many))
    return found


class _Dnsmasq:
    def __init__(self, kill=False):
        self.applied = []
        self.stopped = 0
        self.kill = kill        # the process dies as the segment's DHCP is being switched over

    def apply(self, text):
        if self.kill:
            raise KeyboardInterrupt("killed while switching dnsmasq to the new segment")
        self.applied.append(text)

    def stop(self):
        self.stopped += 1


class _PD:
    def __init__(self):
        self.started = 0
        self.stopped = 0
        self.cleared = 0
        self.callback = None

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def clear_state(self):
        self.cleared += 1

    def set_callback(self, callback):
        self.callback = callback


class _State:
    def __init__(self, store, net, dnsmasq=None, running=False):
        self.store, self.net, self.dnsmasq = store, net, dnsmasq
        self.settings = Settings()
        # The enforcement a pass installs depends on the runtime state (tunnel up => tproxy,
        # otherwise the fail-closed guard), so the fake carries the supervisor that decides it.
        self.supervisor = _Supervisor(running)


# --- Task 1: settings / NetPlan ------------------------------------------------

def test_settings_have_provision_defaults():
    s = Settings()
    assert s.client_dns6 == "2606:4700:4700::1111"
    assert s.dnsmasq_bin == "dnsmasq"
    for k in ("manage_segment", "manage_dnsmasq", "ipv6_pd", "client_dns6"):
        assert k in SETTINGS_DEFAULTS


def test_leasefile_defaults_under_data_dir():
    assert Settings.from_env({}).dnsmasq_leases == "data/dnsmasq.leases"
    assert Settings.from_env({"PI_GW_DATA_DIR": "/data"}).dnsmasq_leases == "/data/dnsmasq.leases"


def test_netplan_carries_client_dns6_and_leasefile():
    p = NetPlan.from_settings(Settings())
    assert p.client_dns6 == "2606:4700:4700::1111"
    assert p.dnsmasq_leases.endswith("dnsmasq.leases")


def test_netplan_from_store_resolves_client_dns6_override():
    store = _store()
    store.set_setting("client_dns6", "2001:4860:4860::8888")
    assert NetPlan.from_store(store, Settings()).client_dns6 == "2001:4860:4860::8888"


# --- Task 2: pure helpers ------------------------------------------------------

def test_parse_vlan():
    assert provision.parse_vlan("eth0.2") == ("eth0", 2)
    assert provision.parse_vlan("eth0") == ("eth0", None)


def test_host_addr6_first_address_in_prefix():
    assert provision.host_addr6("2001:db8:0:2::/64") == "2001:db8:0:2::1/64"
    assert provision.host_addr6("fd00:1:2:3::/64") == "fd00:1:2:3::1/64"
    assert provision.host_addr6("") is None
    assert provision.host_addr6("auto") is None
    assert provision.host_addr6("garbage") is None


def test_host_addr6_rejects_non_64_and_ipv4_prefixes():
    assert provision.host_addr6("2001:db8::/56") is None
    assert provision.host_addr6("2001:db8::/65") is None
    assert provision.host_addr6("192.168.10.0/24") is None


def test_generate_ula_prefix_is_stable_and_encodes_vlan():
    fixed = lambda n: bytes([0xab, 0xcd, 0xef, 0x01, 0x23])[:n]
    p = provision.generate_ula_prefix(2, rand=fixed)
    assert p == "fdab:cdef:123:2::/64"
    net = ipaddress.ip_network(p)
    assert net.prefixlen == 64 and str(net.network_address).startswith("fd")
    assert provision.generate_ula_prefix(5, rand=fixed).endswith(":5::/64")


# --- Task 3: command/file emission ---------------------------------------------

def test_ensure_sysctls_writes_three_knobs():
    writes = []
    provision.ensure_sysctls(Settings(), write_proc=lambda p, v: writes.append((p, v)))
    assert ("/proc/sys/net/ipv4/ip_forward", "1") in writes
    assert ("/proc/sys/net/ipv6/conf/all/forwarding", "1") in writes
    assert ("/proc/sys/net/ipv6/conf/eth0/accept_ra", "2") in writes


def test_ensure_segment_link_creates_vlan_and_records_ownership():
    fake = FakeRun()
    store = _store()
    p = NetPlan.from_settings(Settings())
    provision.ensure_segment_link(store, p, run=fake, link_exists=lambda i: False)
    cmds = fake.cmds()
    assert ["ip", "link", "add", "link", "eth0", "name", "eth0.2", "type", "vlan", "id", "2"] in cmds
    # ...and leaves it DOWN. A link is raised only once the addresses the plan names are on it
    # and everything that constrains the segment has been pointed at it.
    assert not any(c[:3] == ["ip", "link", "set"] for c in cmds)
    assert store.get_setting("managed_segment_link") == "eth0.2"


def test_activate_segment_link_only_brings_the_named_link_up():
    fake = FakeRun()
    provision.activate_segment_link(_plan_for("eth0.9"), run=fake)
    assert fake.cmds() == [["ip", "link", "set", "eth0.9", "up"]]


def test_ensure_segment_link_skips_link_add_and_ownership_when_present():
    fake = FakeRun()
    store = _store()
    p = NetPlan.from_settings(Settings())
    provision.ensure_segment_link(store, p, run=fake, link_exists=lambda i: True)
    assert not any(c[:3] == ["ip", "link", "add"] for c in fake.cmds())
    # a link the panel did not create is never claimed, so disabling never deletes it
    assert store.get_setting("managed_segment_link") in (None, "")


def test_retargeting_the_segment_twice_leaves_no_panel_created_link_behind():
    # A single-valued ledger would overwrite `eth0.2` with `eth0.9` and strand the first link
    # on the host with no owner — nothing would ever delete it.
    host = FakeHost(existing=["eth0"])
    store = _store()

    for iface in ("eth0.2", "eth0.9", "eth0.20"):
        # The two halves of a retarget, in the order a pass runs them: create, then (addresses
        # having landed) retire. `ensure_segment_link` no longer retires anything itself.
        provision.ensure_segment_link(store, _plan_for(iface), run=host,
                                      link_exists=host.exists)
        provision.retire_superseded_links(store, _plan_for(iface), run=host,
                                          link_exists=host.exists)
        assert provision._parse_links(store) == [iface]     # the ledger matches the host
        assert host.links == {"eth0", iface}

    provision.clear_managed_link(store, run=host)

    assert host.links == {"eth0"}
    assert store.get_setting(provision.LINK_KEY) == ""


def test_link_ledger_reads_the_pre_upgrade_single_value_form():
    # A gateway upgrading from the release that stored one bare name here must still have that
    # link recognised and cleaned; silently abandoning it would cause the leak this prevents.
    host = FakeHost(existing=["eth0", "eth0.2"])
    store = _store()
    store.set_setting(provision.LINK_KEY, "eth0.2")

    assert provision._parse_links(store) == ["eth0.2"]
    provision.clear_managed_link(store, run=host)

    assert ["ip", "link", "delete", "eth0.2"] in host.calls
    assert host.links == {"eth0"}
    assert store.get_setting(provision.LINK_KEY) == ""


def test_retargeting_away_from_a_pre_upgrade_recorded_link_removes_it():
    host = FakeHost(existing=["eth0", "eth0.2"])
    store = _store()
    store.set_setting(provision.LINK_KEY, "eth0.2")          # pre-upgrade single value

    provision.ensure_segment_link(store, _plan_for("eth0.9"), run=host,
                                  link_exists=host.exists)
    provision.retire_superseded_links(store, _plan_for("eth0.9"), run=host,
                                      link_exists=host.exists)

    assert host.links == {"eth0", "eth0.9"}
    assert provision._parse_links(store) == ["eth0.9"]


def test_a_link_the_panel_did_not_create_is_never_deleted():
    host = FakeHost(existing=["eth0", "eth0.2"])             # eth0.2 pre-dates the panel
    store = _store()

    provision.ensure_segment_link(store, _plan_for("eth0.2"), run=host,
                                  link_exists=host.exists)
    provision.ensure_segment_link(store, _plan_for("eth0.9"), run=host,
                                  link_exists=host.exists)
    provision.retire_superseded_links(store, _plan_for("eth0.9"), run=host,
                                      link_exists=host.exists)
    provision.clear_managed_link(store, run=host)

    assert ["ip", "link", "delete", "eth0.2"] not in host.calls
    assert "eth0.2" in host.links
    assert host.links == {"eth0", "eth0.2"}


def test_ensure_segment_link_records_the_new_link_before_creating_it():
    # A crash between the two must leave a record of a link that may exist, never a link with
    # no record: the latter is the orphan no later pass would ever delete.
    host = FakeHost(existing=["eth0"])
    store = _store()
    seen: list[str] = []
    real_set = store.set_setting

    def record(key, value):
        real_set(key, value)
        if key == provision.LINK_KEY:
            seen.append(f"set:{value}")

    def run(cmd, input=None):
        if cmd[:3] == ["ip", "link", "add"]:
            seen.append("kernel:add")
        return host(cmd, input)

    store.set_setting = record
    provision.ensure_segment_link(store, _plan_for("eth0.2"), run=run,
                                  link_exists=host.exists)
    assert seen == ["set:eth0.2", "kernel:add"]


def test_a_superseded_link_stays_recorded_until_its_delete_has_run():
    # Between "the new link exists" and "the old one is gone" BOTH belong to the panel;
    # forgetting the old one first would strand it if the process died in that window.
    host = FakeHost(existing=["eth0", "eth0.2"])
    store = _store()
    store.set_setting(provision.LINK_KEY, "eth0.2")
    at_add, at_delete = [], []

    def run(cmd, input=None):
        if cmd[:3] == ["ip", "link", "add"]:
            at_add.append(store.get_setting(provision.LINK_KEY) or "")
        if cmd[:3] == ["ip", "link", "delete"]:
            at_delete.append(store.get_setting(provision.LINK_KEY) or "")
        return host(cmd, input)

    provision.ensure_segment_link(store, _plan_for("eth0.9"), run=run,
                                  link_exists=host.exists)
    provision.retire_superseded_links(store, _plan_for("eth0.9"), run=run,
                                      link_exists=host.exists)

    assert at_add == ["eth0.2\neth0.9"]
    assert at_delete == ["eth0.2\neth0.9"]
    assert store.get_setting(provision.LINK_KEY) == "eth0.9"


def test_clear_managed_link_drains_every_recorded_link():
    host = FakeHost(existing=["eth0", "eth0.2", "eth0.9"])
    store = _store()
    store.set_setting(provision.LINK_KEY, "eth0.2\neth0.9")

    provision.clear_managed_link(store, run=host)

    assert host.links == {"eth0"}
    assert store.get_setting(provision.LINK_KEY) == ""


def test_clear_managed_link_forgets_an_already_gone_link():
    # Reboot/manual cleanup: the delete fails, but the ownership entry must still drain or the
    # ledger grows a name that can never be retired. Absence is what licenses that — the probe
    # confirms it — and a delete failing for this reason is not a provisioning problem.
    host = FakeHost(existing=["eth0"])
    store = _store()
    store.set_setting(provision.LINK_KEY, "eth0.2\neth0.9")

    failed = provision.clear_managed_link(store, run=host)

    assert failed == []
    assert store.get_setting(provision.LINK_KEY) == ""


def test_a_link_that_would_not_delete_stays_owned_and_is_reported():
    # "Already gone" and "the delete was refused" are different facts. Reading the second as the
    # first drops ownership of a link that is still up — the very orphan the ledger prevents,
    # reintroduced through the error path — so absence must be proven before the entry goes.
    host = RefusingHost(existing=["eth0", "eth0.2"], refuse=["eth0.2"])
    store = _store()
    store.set_setting(provision.LINK_KEY, "eth0.2")

    provision.ensure_segment_link(store, _plan_for("eth0.9"), run=host,
                                  link_exists=host.exists)
    failed = provision.retire_superseded_links(store, _plan_for("eth0.9"), run=host,
                                               link_exists=host.exists)

    assert host.links == {"eth0", "eth0.2", "eth0.9"}        # the superseded link is still up
    assert provision._parse_links(store) == ["eth0.2", "eth0.9"]     # so it is still owned
    assert len(failed) == 1 and failed[0].startswith("eth0.2: ")
    assert "not permitted" in failed[0]


def test_a_timed_out_delete_is_treated_as_unknown_not_as_gone():
    # The bounded runner reports a delete that never returned as CalledProcessError(124). That
    # exit status carries no information about the link, so it may not clear ownership.
    host = RefusingHost(existing=["eth0", "eth0.2"], refuse=["eth0.2"],
                        returncode=TIMEOUT_RETURNCODE,
                        stderr="command timed out after 10s: ip link delete eth0.2")
    store = _store()
    store.set_setting(provision.LINK_KEY, "eth0.2")

    failed = provision.clear_managed_link(store, run=host, link_exists=host.exists)

    assert host.links == {"eth0", "eth0.2"}
    assert provision._parse_links(store) == ["eth0.2"]       # retained, so a later run retries
    assert len(failed) == 1 and "timed out" in failed[0]


def test_host_provision_reports_a_panel_created_link_it_could_not_remove():
    # The operator has to learn about it: a link the panel owns and left running means the pass
    # did not reach the host state it would otherwise report as applied.
    host = RefusingHost(existing=["eth0", "eth0.2"], refuse=["eth0.2"])
    store = _store()
    store.set_setting(provision.LINK_KEY, "eth0.2")
    store.set_setting("segment_iface", "eth0.9")
    state = _State(store, LinuxBackend(host), _Dnsmasq())

    result = provision.host_provision(state)

    assert not result.ok
    assert "eth0.2" in result.error and "not removed" in result.error
    assert state.provision_result is result
    # The new segment still comes up — the leftover is reported, not allowed to abort the pass.
    assert "eth0.9" in host.links and state.dnsmasq.applied


# --- the DEFAULT probe seam: what "absent" is allowed to mean -------------------------------
# The tests above inject a bool `link_exists`, which is a test's own authoritative host model.
# Production injects nothing, and the default seam is where the distinction between "not there"
# and "could not tell" actually has to be drawn — so these drive it with no seam at all.


def test_the_default_probe_calls_only_an_explicit_not_found_absent():
    # One state per answer `ip link show` can give. A timeout and an EPERM are NOT absence: the
    # exit status says nothing about the device, and reading either as "gone" is what lets a live
    # VLAN lose its owner.
    host = FakeHost(existing=["eth0"])
    assert provision._probe_link("eth0", run=host)[0] == provision.LINK_PRESENT
    assert provision._probe_link("eth0.2", run=host)[0] == provision.LINK_ABSENT

    def timed_out(cmd, input=None):
        raise subprocess.CalledProcessError(
            TIMEOUT_RETURNCODE, cmd, output="", stderr=f"command timed out: {' '.join(cmd)}")

    def refused(cmd, input=None):
        raise subprocess.CalledProcessError(
            1, cmd, output="", stderr="RTNETLINK answers: Operation not permitted")

    def broken(cmd, input=None):
        raise OSError("ip: command not found")

    for run in (timed_out, refused, broken):
        state, reason = provision._probe_link("eth0.2", run=run)
        assert state == provision.LINK_UNKNOWN, f"{run.__name__} was read as an answer"
        assert reason, f"{run.__name__} reported an unknown with no reason"


# --- ...and what "absent" is allowed to mean for an ADDRESS ---------------------------------
# `ADDR_ABSENT` is asked exactly once: after an `ip addr del` the panel issued against an address
# it owns failed. It is the one answer that says the removal has nothing left to do, and so the one
# answer that licenses forgetting the record that would otherwise retry it. It may therefore only
# ever come from an answer the probe actually READ.

@pytest.mark.parametrize("unreadable", [
    None,                                                   # the seam returned nothing at all
    subprocess.CompletedProcess(["ip"], 0, None, ""),        # ran, but captured no stdout
    object(),                                               # a mock: no `stdout` attribute
    b"192.168.10.2/24",                                     # bytes: `text=True` was not in force
])
def test_a_probe_whose_output_cannot_be_read_is_unknown_and_never_absence(unreadable):
    # "No output" and "the output says nothing is there" are different facts. Reading the first as
    # the second retires the ownership record of an address that is still on the host, leaving it
    # with nothing to retry the removal and — with segment management off — nothing to report it.
    state, reason = provision._probe_addr("eth0.2", "192.168.10.2/24",
                                          run=lambda cmd, input=None: unreadable)
    assert state == provision.ADDR_UNKNOWN, f"{unreadable!r} was read as an answer"
    assert reason, "an unknown was reported with no reason"


def test_a_probe_that_really_listed_nothing_is_still_absence():
    # The other direction: a readable listing that does not contain the address IS absence, and a
    # readable one that does is presence. Neither may be lost to the rule above.
    listed = ("2: eth0.2    inet 192.168.10.2/24 scope global eth0.2\n"
              "       valid_lft forever preferred_lft forever\n")
    assert provision._probe_addr(
        "eth0.2", "192.168.10.2/24",
        run=lambda cmd, input=None: subprocess.CompletedProcess(cmd, 0, "", ""))[0] \
        == provision.ADDR_ABSENT
    assert provision._probe_addr(
        "eth0.2", "192.168.10.2/24",
        run=lambda cmd, input=None: subprocess.CompletedProcess(cmd, 0, listed, ""))[0] \
        == provision.ADDR_PRESENT
    # a seam that hands the text back directly (a backend's own `_run`) still reads as an answer
    assert provision._probe_addr("eth0.2", "192.168.10.2/24",
                                 run=lambda cmd, input=None: listed)[0] == provision.ADDR_PRESENT


def test_a_delete_and_a_probe_that_both_time_out_leave_the_link_owned():
    # The production shape of the bug: the runner's time limit fires on the delete AND on the
    # `ip link show` that would have proven the link gone. Neither says anything about the
    # device, so the ledger entry must survive — dropping it strands a live VLAN with no owner
    # and no later pass would ever look at it again.
    host = UnansweringHost(existing=["eth0", "eth0.2"], unanswered=["eth0.2"])
    store = _store()
    store.set_setting(provision.LINK_KEY, "eth0.2")

    failed = provision.clear_managed_link(store, run=host)          # DEFAULT seam

    assert host.links == {"eth0", "eth0.2"}                          # still up
    assert provision._parse_links(store) == ["eth0.2"]               # still owned, so it retries
    assert len(failed) == 1 and failed[0].startswith("eth0.2: ")
    assert "timed out" in failed[0] and "could not be probed" in failed[0]


def test_a_link_proven_absent_through_the_default_seam_is_still_forgotten():
    # The other direction, on the same fake: a delete that fails because the link really is gone
    # gets an explicit not-found from `ip link show`, and that entry MUST drain or the ledger
    # grows a name nothing can ever retire.
    host = UnansweringHost(existing=["eth0"], unanswered=["eth0.9"])
    store = _store()
    store.set_setting(provision.LINK_KEY, "eth0.2\neth0.9")

    failed = provision.clear_managed_link(store, run=host)          # DEFAULT seam

    assert provision._parse_links(store) == ["eth0.9"]   # only the unanswerable one is retained
    assert len(failed) == 1 and failed[0].startswith("eth0.9: ")


def test_host_provision_reports_a_link_whose_probe_could_not_answer():
    # host_provision never injects a seam, so this is the whole chain on the default probe: an
    # unanswerable link is a failed pass, which is what rolls the candidate settings back.
    host = UnansweringHost(existing=["eth0", "eth0.2"], unanswered=["eth0.2"])
    store = _store()
    store.set_setting(provision.LINK_KEY, "eth0.2")
    store.set_setting("segment_iface", "eth0.9")
    state = _State(store, LinuxBackend(host), _Dnsmasq())

    result = provision.host_provision(state)

    assert not result.ok
    assert "eth0.2" in result.error and "not removed" in result.error
    assert provision._parse_links(store) == ["eth0.2", "eth0.9"]
    assert "eth0.9" in host.links                    # the new segment still came up


def test_reconcile_segment_addresses_replaces_only_panel_owned_addresses():
    fake = FakeRun()
    store = _store()
    store.set_setting("managed_segment_iface", "eth0.2")
    store.set_setting("managed_segment_addr4", "192.168.9.2/24")
    store.set_setting("managed_segment_addr6", "fd00:1:2:9::1/64")
    plan = NetPlan.from_settings(Settings())
    plan.ipv6_enabled = True
    plan.segment_ip6 = "fd00:1:2:3::/64"

    provision.reconcile_segment_addresses(store, plan, run=fake)

    cmds = fake.cmds()
    assert ["ip", "addr", "replace", "192.168.10.2/24", "dev", "eth0.2"] in cmds
    assert ["ip", "-6", "addr", "replace", "fd00:1:2:3::1/64", "dev", "eth0.2"] in cmds
    assert ["ip", "addr", "del", "192.168.9.2/24", "dev", "eth0.2"] in cmds
    assert ["ip", "-6", "addr", "del", "fd00:1:2:9::1/64", "dev", "eth0.2"] in cmds
    assert not any("flush" in cmd for cmd in cmds)
    assert store.get_setting("managed_segment_addr4") == "192.168.10.2/24"
    assert store.get_setting("managed_segment_addr6") == "fd00:1:2:3::1/64"


def test_reconcile_segment_addresses_removes_managed_v6_when_disabled():
    fake = FakeRun()
    store = _store()
    store.set_setting("managed_segment_iface", "eth0.2")
    store.set_setting("managed_segment_addr6", "fd00:1:2:3::1/64")
    plan = NetPlan.from_settings(Settings())

    provision.reconcile_segment_addresses(store, plan, run=fake)

    assert ["ip", "-6", "addr", "del", "fd00:1:2:3::1/64", "dev", "eth0.2"] in fake.cmds()
    assert store.get_setting("managed_segment_addr6") in (None, "")


def test_reconcile_records_ownership_before_touching_the_kernel():
    # The caller may run this inside a DB transaction that later rolls back. If the ownership
    # write trails the `ip addr replace`, the rolled-back record no longer names the address
    # left on the host and nothing ever removes it.
    fake = FakeRun()
    store = _store()
    seen: list[str] = []
    real_set = store.set_setting

    def record(key, value):
        real_set(key, value)
        if key == "managed_segment_addr4":
            seen.append(f"set:{value}")

    def run(cmd, input=None):
        if cmd[:3] == ["ip", "addr", "replace"]:
            seen.append(f"kernel:{cmd[3]}")
        return fake(cmd, input)

    store.set_setting = record
    provision.reconcile_segment_addresses(store, NetPlan.from_settings(Settings()), run=run)
    assert seen.index("set:192.168.10.2/24") < seen.index("kernel:192.168.10.2/24")


def test_reconcile_keeps_a_replaced_address_recorded_until_it_is_deleted():
    # Between "the new address is on the host" and "the old one is gone" BOTH are the panel's;
    # dropping the old record first would strand it if the process died in that window.
    store = _store()
    store.set_setting("managed_segment_iface", "eth0.2")
    store.set_setting("managed_segment_addr4", "192.168.9.2/24")
    recorded: list[str] = []

    def run(cmd, input=None):
        if cmd[:3] == ["ip", "addr", "replace"]:
            recorded.append(store.get_setting(provision.STALE_KEY) or "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    provision.reconcile_segment_addresses(store, NetPlan.from_settings(Settings()), run=run)
    assert recorded == ["eth0.2 192.168.9.2/24"]
    assert store.get_setting(provision.STALE_KEY) == ""      # drained once the delete ran


# --- a removal that did not happen may not clear the record that would retry it --------------
# The reconcile and clear paths DO delete: those addresses are the panel's own, recorded before it
# installed them, and replacing or dropping them is the whole job. What they used to do wrong is
# discard the `CalledProcessError` and clear the ownership record anyway, so a refused or timed-out
# `ip addr del` left the address on the interface with nothing recording it, nothing to retry it,
# and — with segment management off, where readiness skips the address check — nothing reporting it.

REFUSALS = [
    ((1, "RTNETLINK answers: Operation not permitted"), "Operation not permitted"),
    ((TIMEOUT_RETURNCODE, "command timed out after 10s: ip addr del 192.168.9.2/24 dev eth0.2"),
     "timed out"),
]


@pytest.mark.parametrize("refused_as, expected", REFUSALS)
def test_reconcile_retains_a_replaced_address_whose_deletion_was_refused(refused_as, expected):
    host = AddrHost(addrs=["192.168.9.2/24"], refuse=["192.168.9.2/24"],
                    returncode=refused_as[0], stderr=refused_as[1])
    store = _store()
    store.set_setting("managed_segment_iface", "eth0.2")
    store.set_setting("managed_segment_addr4", "192.168.9.2/24")

    outcome = provision.reconcile_segment_addresses(
        store, NetPlan.from_settings(Settings()), run=host)

    assert ["ip", "addr", "del", "192.168.9.2/24", "dev", "eth0.2"] in host.cmds()  # it was tried
    assert host.addrs == {"192.168.9.2/24", "192.168.10.2/24"}                      # ...and stayed
    assert store.get_setting(provision.STALE_KEY) == "eth0.2 192.168.9.2/24"    # still owned
    assert outcome.applied is True              # the desired address IS on the interface
    assert len(outcome.reasons) == 1 and expected in outcome.reasons[0]
    assert "192.168.9.2/24" in outcome.reasons[0] and "eth0.2" in outcome.reasons[0]


def test_reconcile_forgets_an_address_a_failed_delete_proves_is_already_gone():
    # The other direction, and why the record may ever be dropped on a failure at all: the kernel
    # lost the address (a reboot), so the delete fails and the probe afterwards says it is not
    # there. Keeping it would grow a ledger entry nothing can ever retire.
    host = AddrHost(refuse=["192.168.9.2/24"], stderr="RTNETLINK answers: Cannot assign requested address")
    store = _store()
    store.set_setting("managed_segment_iface", "eth0.2")
    store.set_setting("managed_segment_addr4", "192.168.9.2/24")

    outcome = provision.reconcile_segment_addresses(
        store, NetPlan.from_settings(Settings()), run=host)

    assert outcome.applied is True and outcome.reasons == []
    assert store.get_setting(provision.STALE_KEY) == ""


def test_reconcile_keeps_the_record_when_the_probe_after_a_refusal_cannot_answer():
    # A delete that failed AND a probe that says nothing about the interface is the state most
    # likely to be sitting on an address that is still there, so the record survives and the
    # reason names both halves.
    def run(cmd, input=None):
        if cmd[:3] == ["ip", "addr", "del"]:
            raise subprocess.CalledProcessError(1, cmd, output="", stderr="RTNETLINK: refused")
        if "show" in cmd:
            raise subprocess.CalledProcessError(TIMEOUT_RETURNCODE, cmd, output="",
                                                stderr="command timed out after 10s")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    store = _store()
    store.set_setting("managed_segment_iface", "eth0.2")
    store.set_setting("managed_segment_addr4", "192.168.9.2/24")

    outcome = provision.reconcile_segment_addresses(
        store, NetPlan.from_settings(Settings()), run=run)

    assert store.get_setting(provision.STALE_KEY) == "eth0.2 192.168.9.2/24"
    assert len(outcome.reasons) == 1 and "could not be probed" in outcome.reasons[0]


def test_host_provision_reports_an_address_the_clear_path_could_not_remove():
    # The whole chain, with segment management off — the one case where `clear_managed_addresses`
    # runs, and the one where readiness deliberately skips the segment-address check. The pass
    # result is therefore the only surface a leftover can appear on, and `/api/ready`'s
    # `provisioning` check reads exactly this object.
    host = AddrHost(addrs=["192.168.10.2/24"], refuse=["192.168.10.2/24"])
    store = _store()
    store.set_setting("manage_segment", "0")
    store.set_setting("managed_segment_iface", "eth0.2")
    store.set_setting("managed_segment_addr4", "192.168.10.2/24")
    state = _State(store, LinuxBackend(host), _Dnsmasq())

    result = provision.host_provision(state)

    assert not result.ok and state.provision_result is result
    assert "192.168.10.2/24" in result.error and "not removed" in result.error
    assert host.addrs == {"192.168.10.2/24"}                       # still on the host
    assert store.get_setting(provision.STALE_KEY) == "eth0.2 192.168.10.2/24"   # still owned
    assert store.get_setting("managed_segment_addr4") in (None, "")


def test_the_clear_path_retries_a_retained_address_on_the_next_pass():
    # What retaining it buys: the same path runs on every later pass while management is off, and
    # the entry it kept is what makes the second attempt happen at all.
    host = AddrHost(addrs=["192.168.10.2/24"], refuse=["192.168.10.2/24"])
    store = _store()
    store.set_setting("managed_segment_iface", "eth0.2")
    store.set_setting("managed_segment_addr4", "192.168.10.2/24")

    assert provision.clear_managed_addresses(store, run=host)      # refused, so retained
    host.refuse.clear()                                            # the next pass gets through
    assert provision.clear_managed_addresses(store, run=host) == []

    assert host.addrs == set()
    assert store.get_setting(provision.STALE_KEY) == ""


def test_clear_managed_addresses_also_drains_a_stranded_stale_entry():
    fake = FakeRun()
    store = _store()
    store.set_setting("managed_segment_iface", "eth0.2")
    store.set_setting("managed_segment_addr4", "192.168.10.2/24")
    store.set_setting(provision.STALE_KEY, "eth0.2 192.168.9.2/24\neth0.2 fd00:1:2:9::1/64")

    provision.clear_managed_addresses(store, run=fake)

    cmds = fake.cmds()
    assert ["ip", "addr", "del", "192.168.9.2/24", "dev", "eth0.2"] in cmds
    assert ["ip", "-6", "addr", "del", "fd00:1:2:9::1/64", "dev", "eth0.2"] in cmds
    assert ["ip", "addr", "del", "192.168.10.2/24", "dev", "eth0.2"] in cmds
    assert store.get_setting(provision.STALE_KEY) == ""


# --- the ledger may never name an address the pass is INSTALLING -----------------------------
# Retaining a pair whose removal was refused is what gets the removal retried. But the retained
# thing is an ADDRESS, and a later change can make that same address the desired one again: A -> B
# with A's removal refused, then B -> A. The pass installs A (correctly — A is desired), and a
# retention mechanism that does not check then dutifully deletes A, because A is still on the stale
# ledger, clears the record, and reports no failure. That leaves the segment interface with NO
# address, silently, which is the operator's network down. So the desired pairs are excluded from
# the stale set in both places it is handled: where it is recorded, and where it is retired.


def _plan_with(ip4: str, ip6: str = "") -> NetPlan:
    plan = NetPlan.from_settings(Settings())
    plan.segment_ip = ip4
    if ip6:
        plan.ipv6_enabled, plan.segment_ip6 = True, ip6
    return plan


def test_coming_back_to_a_retained_address_keeps_it_on_the_interface():
    # The reproduction, end to end, against the host model: A retained by a refused delete, the
    # config moved to B, then back to A. What must be true afterwards is about the INTERFACE, not
    # about the absence of an exception: the segment still carries the address it is configured for.
    host = AddrHost(addrs=["192.168.9.2/24"], refuse=["192.168.9.2/24"])
    store = _store()
    store.set_setting("managed_segment_iface", "eth0.2")
    store.set_setting("managed_segment_addr4", "192.168.9.2/24")

    # A -> B: the removal of A is refused, so A stays recorded and the pass says so.
    assert provision.reconcile_segment_addresses(
        store, _plan_with("192.168.10.2"), run=host).reasons
    assert store.get_setting(provision.STALE_KEY) == "eth0.2 192.168.9.2/24"

    # B -> A: A is the desired address again, and whatever refused its removal has cleared.
    host.refuse.clear()
    mark = len(host.calls)
    outcome = provision.reconcile_segment_addresses(store, _plan_with("192.168.9.2"), run=host)

    later = host.cmds()[mark:]
    assert ["ip", "addr", "replace", "192.168.9.2/24", "dev", "eth0.2"] in later
    assert not any(cmd[:3] == ["ip", "addr", "del"] and cmd[3] == "192.168.9.2/24"
                   for cmd in later)                     # never deleted while it is the desired one
    assert host.addrs == {"192.168.9.2/24"}              # ON the interface; B correctly retired
    assert store.get_setting("managed_segment_addr4") == "192.168.9.2/24"
    assert store.get_setting(provision.STALE_KEY) == ""
    assert outcome.applied is True and outcome.reasons == []


def test_the_recorded_ledger_never_repeats_a_pair_or_names_a_desired_one():
    # The ledger is written BEFORE the kernel is touched, deliberately, so what it says at that
    # instant is what a pass dying there leaves for the next one to act on. Two things it may never
    # say: that the panel owes a removal of the address it is installing, and the same pair twice.
    old, desired = "eth0.2 192.168.9.2/24", "eth0.2 192.168.10.2/24"
    store = _store()
    store.set_setting("managed_segment_iface", "eth0.2")
    store.set_setting("managed_segment_addr4", "192.168.9.2/24")
    store.set_setting(provision.STALE_KEY, f"{old}\n{old}\n{desired}")
    seen: list[str] = []

    def run(cmd, input=None):
        if cmd[:3] == ["ip", "addr", "replace"]:
            seen.append(store.get_setting(provision.STALE_KEY) or "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    provision.reconcile_segment_addresses(store, _plan_with("192.168.10.2"), run=run)

    assert seen == [old]        # the desired pair is not owed a removal; the repeat collapsed


def test_retire_owned_never_deletes_a_pair_the_pass_is_installing():
    # The second half of the exclusion, at the module's only address-deletion site: whatever a
    # caller assembles, a pair the pass is installing is not deleted. Excluding it forgets nothing
    # — a desired address is recorded as the segment's current one, not as one owed a removal.
    host = AddrHost(addrs=["192.168.10.2/24", "192.168.9.2/24"])

    keep, reasons = provision._retire_owned(
        [("eth0.2", "192.168.10.2/24"), ("eth0.2", "192.168.9.2/24")], host,
        provision._desired_pairs("eth0.2", "192.168.10.2/24"))

    assert keep == [] and reasons == []
    assert host.addrs == {"192.168.10.2/24"}                    # the desired one is untouched
    assert not any(cmd[:3] == ["ip", "addr", "del"] and cmd[3] == "192.168.10.2/24"
                   for cmd in host.cmds())


def test_a_genuine_move_still_deletes_the_address_it_replaced():
    # The behaviour that is NOT being changed: a segment that genuinely moves has its old address
    # removed, because the panel owns it and it is no longer desired.
    host = AddrHost(addrs=["192.168.9.2/24", "fd00:1:2:9::1/64"])
    store = _store()
    store.set_setting("managed_segment_iface", "eth0.2")
    store.set_setting("managed_segment_addr4", "192.168.9.2/24")
    store.set_setting("managed_segment_addr6", "fd00:1:2:9::1/64")

    outcome = provision.reconcile_segment_addresses(
        store, _plan_with("192.168.10.2", "fd00:1:2:3::/64"), run=host)

    assert outcome.applied is True and outcome.reasons == []
    assert host.addrs == {"192.168.10.2/24", "fd00:1:2:3::1/64"}
    assert store.get_setting(provision.STALE_KEY) == ""


# --- an address is somewhere, and the probe is asked about ONE interface ----------------------
# Both of these are questions the fake could not be asked while `ip addr show dev <iface>`
# answered from a flat set of everything the host carried: an address on ANOTHER interface came
# back as present here, which is the answer that keeps an ownership record, and a refusal could
# only be keyed by CIDR, which cannot express a delete that is refused on the interface the pass
# is leaving while the same CIDR goes on fine on the one it is moving to.


def test_a_refused_removal_forgets_the_record_when_the_address_is_on_a_DIFFERENT_interface():
    # The panel owns `192.168.9.2/24` on eth0.2; what is on the host is the same CIDR on eth0.9,
    # put there by someone else. The delete is refused, and the probe that follows asks about
    # eth0.2 alone: the address is not there, so this record has nothing left to retry and goes.
    # Keeping it — which a probe answering "somewhere on this host" produces — would make the
    # panel retry a removal forever against an interface that never had the address.
    host = AddrHost(on={"eth0.9": {"192.168.9.2/24"}},
                    refuse=[("eth0.2", "192.168.9.2/24")])

    keep, reasons = provision._retire_owned([("eth0.2", "192.168.9.2/24")], host)

    assert keep == [] and reasons == []
    assert host.addrs_on("eth0.9") == {"192.168.9.2/24"}     # and the other interface is untouched


def test_the_same_address_moving_interface_is_refused_only_where_it_is_being_removed():
    # A retarget that keeps the segment address and changes only the interface. The replace onto
    # eth0.9 has to succeed while the delete from eth0.2 is refused — one CIDR, two interfaces,
    # opposite answers — so the pass ends with the address on BOTH, applied, and the OLD pair
    # still recorded as owed a removal.
    host = AddrHost(on={"eth0.2": {"192.168.10.2/24"}},
                    refuse=[("eth0.2", "192.168.10.2/24")])
    store = _store()
    store.set_setting("segment_iface", "eth0.9")
    store.set_setting("managed_segment_iface", "eth0.2")
    store.set_setting("managed_segment_addr4", "192.168.10.2/24")
    plan = _plan_with("192.168.10.2")
    plan.segment_iface = "eth0.9"

    outcome = provision.reconcile_segment_addresses(store, plan, run=host)

    assert outcome.applied is True and len(outcome.reasons) == 1
    assert outcome.reasons[0].startswith("192.168.10.2/24 on eth0.2: ")
    assert host.addrs_on("eth0.9") == {"192.168.10.2/24"}    # installed where the plan wants it
    assert host.addrs_on("eth0.2") == {"192.168.10.2/24"}    # and still where it would not go
    assert provision._parse_stale(store) == [("eth0.2", "192.168.10.2/24")]
    assert store.get_setting("managed_segment_addr4") == "192.168.10.2/24"


# --- what a pass DID is two facts, and neither is a list of warnings --------------------------
# `reconcile_segment_addresses` used to answer with a list of reasons, which cannot say whether
# anything was applied: "installed, but a superseded address would not go" and "not installed, so
# the interface still has the OLD address" were the same non-empty list. Both callers read a
# non-empty list as a warning and went on to configure dnsmasq for the new plan — a DHCP range, a
# router option and a listen address all derived from `plan.segment_ip` — over an interface that did
# not have that address. The outcome carries `applied` separately, and nothing keyed to the plan
# runs when it is False.


class UninstallableAddrHost(AddrHost):
    """An `AddrHost` whose `ip addr replace` is REFUSED for the addresses named in `reject`.

    With the ownership ceiling gone this is the only way a pass ends with `applied=False`: the
    kernel would not take the address. (`AddrHost.refuse` models the other half — a `del` the
    kernel would not take, which is a warning and not a failure to apply.) `reject` is keyed the
    same way `refuse` is: a CIDR refuses that address on any interface, an `(iface, CIDR)` pair
    only on that one.
    """

    def __init__(self, addrs=(), reject=(), **kwargs):
        super().__init__(addrs=addrs, **kwargs)
        self.reject = set(reject)

    def __call__(self, cmd, input=None):
        rest = _strip_flags(cmd)
        if rest[:2] == ["addr", "replace"] and self.blocked(self.reject, rest[4], rest[2]):
            self.calls.append(cmd)
            raise subprocess.CalledProcessError(1, cmd, output="",
                                                stderr="RTNETLINK answers: Permission denied")
        return super().__call__(cmd, input)


def test_the_address_outcome_will_not_be_read_as_a_bare_list_of_warnings():
    not_applied = provision.AddressOutcome(False, ["the addresses were not installed"])
    warned = provision.AddressOutcome(True, ["could not remove the old address"])

    assert not_applied.applied is False and warned.applied is True
    with pytest.raises(TypeError):      # both are non-empty: truthiness cannot tell them apart
        bool(not_applied)
    with pytest.raises(TypeError):      # nor can anything treating it as the old list of reasons
        "; ".join(warned)


def test_addresses_that_never_landed_do_not_configure_dnsmasq_for_a_subnet_the_host_lacks():
    # The product consequence, end to end. The kernel refuses the new address, so the interface
    # keeps 192.168.9.2/24 — and dnsmasq must not be told to serve the 192.168.10.0/24 segment, nor
    # may the NetworkManager drop-in be moved, because neither is true of this host.
    host = UninstallableAddrHost(addrs=["192.168.9.2/24"], reject=["192.168.10.2/24"])
    store = _store()
    store.set_setting("segment_ip", "192.168.10.2")
    store.set_setting("managed_segment_iface", "eth0.2")
    store.set_setting("managed_segment_addr4", "192.168.9.2/24")
    state = _State(store, LinuxBackend(host), _Dnsmasq())

    result = provision.host_provision(state)

    assert not result.ok and "not applied" in result.error
    assert state.dnsmasq.applied == [] and state.dnsmasq.stopped == 0
    assert not any("nsenter" in cmd for cmd in host.cmds())      # no NM drop-in reload either
    assert host.addrs == {"192.168.9.2/24"}                      # the segment keeps what it has
    # And the address it could not install is NOT retired out from under the interface.
    assert not any(cmd[:3] == ["ip", "addr", "del"] and cmd[3] == "192.168.9.2/24"
                   for cmd in host.cmds())


def test_a_cleanup_failure_still_configures_dnsmasq_because_the_address_is_installed():
    # The other outcome, which the same list of reasons used to be indistinguishable from: the new
    # address IS on the interface and only the old one would not go, so everything keyed to the
    # plan must still run. The pass still fails — the leftover is still reported — but differently.
    host = AddrHost(addrs=["192.168.9.2/24"], refuse=["192.168.9.2/24"])
    store = _store()
    store.set_setting("segment_ip", "192.168.10.2")
    store.set_setting("managed_segment_iface", "eth0.2")
    store.set_setting("managed_segment_addr4", "192.168.9.2/24")
    state = _State(store, LinuxBackend(host), _Dnsmasq())

    result = provision.host_provision(state)

    assert not result.ok
    assert "not removed" in result.error and "not applied" not in result.error
    assert len(state.dnsmasq.applied) == 1 and "192.168.10.2" in state.dnsmasq.applied[0]
    assert host.addrs == {"192.168.9.2/24", "192.168.10.2/24"}
    assert store.get_setting("managed_segment_addr4") == "192.168.10.2/24"


# --- an undrained backlog is REPORTED, and never refuses the operator's change ------------------
# Every distinct address whose removal is refused stays on the ledger, and a DHCPv6-PD prefix can
# renew over and over with no operator action, so a long backlog is reachable in normal operation.
# It used to be CAPPED, and the cap worked by declining the change. That put a "do nothing" branch
# in the middle of a multi-step host reconfiguration — where `ensure_segment_link` had already
# retired the superseded VLAN — so a retarget at the cap deleted the old link and its address and
# then installed nothing, destroying the configuration it was meant to preserve. The cap is gone,
# and so is that ordering (see the retarget section at the end of this file).
# Growth is now named through the same result that fails `/api/ready`, and nothing else changes.


def _backlog(count: int, first: int = 20) -> list[str]:
    return [f"eth0.2 192.168.{n}.2/24" for n in range(first, first + count)]


def _owned_set(store) -> set[tuple[str, str]]:
    """Every `(iface, addr)` pair the panel currently claims.

    The backlog it owes a removal for, PLUS the addresses it records as the segment's own. Both
    halves matter: the clear path moves the second into the first and blanks the keys, so a test
    that watched only one of them would see pairs vanish that had merely changed sides.
    """
    iface = store.get_setting("managed_segment_iface") or ""
    pairs = set(provision._parse_stale(store))
    for addr in (store.get_setting("managed_segment_addr4") or "",
                 store.get_setting("managed_segment_addr6") or ""):
        if iface and addr:
            pairs.add((iface, addr))
    return pairs


class RefusingAddrHost(AddrHost):
    """An `AddrHost` on which every `ip addr del` fails, whatever it names, leaving the address.

    `refuse` has to be listed up front, which cannot express a host that refuses addresses the test
    has not chosen yet — the clear/change cycle installs a new pair every round. Setting `refusing`
    to False hands the host back to the ordinary behaviour, which is how the recovery half of that
    test proves the state is escapable without touching the store.
    """

    def __init__(self, addrs=(), **kwargs):
        super().__init__(addrs=addrs, **kwargs)
        self.refusing = True

    def __call__(self, cmd, input=None):
        rest = _strip_flags(cmd)
        if self.refusing and rest[:2] == ["addr", "del"]:
            self.refuse.add(rest[2])
        return super().__call__(cmd, input)


class LinkAndAddrHost(AddrHost):
    """An `AddrHost` that also models the LINKS, so a retarget can be watched end to end.

    The retarget is where the removed ceiling did its worst, and watching it needs both halves of
    the host in one runner: whether the new link ends up ADDRESSED, and whether the old one is
    still there when it does not, is the whole question.
    """

    def __init__(self, links=(), **kwargs):
        super().__init__(**kwargs)
        self.links = set(links)

    def __call__(self, cmd, input=None):
        norm = ["ip"] + _strip_flags(cmd)
        if norm[:3] == ["ip", "link", "add"]:
            self.calls.append(cmd)
            self.links.add(norm[norm.index("name") + 1])
            self.note_link(norm)                # created DOWN, and recorded as such
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if norm[:3] == ["ip", "link", "delete"]:
            self.calls.append(cmd)
            self.links.discard(norm[3])
            self.note_link(norm)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if norm[:3] == ["ip", "link", "show"] and norm[3] not in self.links:
            self.calls.append(cmd)
            raise subprocess.CalledProcessError(
                1, cmd, output="", stderr=f'Device "{norm[3]}" does not exist.')
        return super().__call__(cmd, input)


def test_an_interface_retarget_with_a_large_backlog_still_applies_in_full():
    # THE reason the ceiling is gone. The superseded VLAN used to be retired before the addresses
    # were reconciled, so a pass that then declined left the segment with neither the old link and
    # its address nor the new ones. A backlog may not produce that: the retarget lands whole —
    # new link, new address, dnsmasq, NM — however long the ledger is.
    backlog = _backlog(provision.BACKLOG_WARN * 3)
    owed = {line.split()[1] for line in backlog}
    host = LinkAndAddrHost(links={"eth0", "eth0.2"}, addrs=owed | {"192.168.9.2/24"}, refuse=owed)
    store = _store()
    store.set_setting("segment_iface", "eth0.9")
    store.set_setting("segment_ip", "192.168.10.2")
    store.set_setting("managed_segment_iface", "eth0.2")
    store.set_setting("managed_segment_addr4", "192.168.9.2/24")
    store.set_setting(provision.LINK_KEY, "eth0.2")
    store.set_setting(provision.STALE_KEY, "\n".join(backlog))
    state = _State(store, LinuxBackend(host), _Dnsmasq())

    result = provision.host_provision(state)

    assert "eth0.9" in host.links and "eth0.2" not in host.links   # the retarget happened...
    assert "192.168.10.2/24" in host.addrs                          # ...and the new link IS addressed
    assert ["ip", "addr", "replace", "192.168.10.2/24", "dev", "eth0.9"] in host.cmds()
    assert len(state.dnsmasq.applied) == 1 and "192.168.10.2" in state.dnsmasq.applied[0]
    assert any("nsenter" in cmd for cmd in host.cmds())             # the NM drop-in moved with it
    assert store.get_setting("managed_segment_iface") == "eth0.9"
    # The new link carries the plan's address and nothing else; the superseded VLAN is gone and
    # took everything that was on it — including the backlog, which the host really does lose with
    # the interface. Not one of those pairs is FORGOTTEN, though: they stay owed a removal below.
    assert host.addrs_on("eth0.9") == {"192.168.10.2/24"}
    assert host.addrs_on("eth0.2") == set()
    assert {addr for _, addr in provision._parse_stale(store)} == owed
    # It still FAILS — the undrained backlog is reported — but as a cleanup problem, never as a
    # change the panel declined to make.
    assert not result.ok and "not applied" not in result.error
    assert "not removed" in result.error


def test_a_large_backlog_is_reported_by_count_and_shows_up_in_readiness():
    # Visible, not fatal. With segment management off the clear path runs and readiness skips its
    # address check entirely, so this result is the ONLY surface the backlog can appear on — and
    # `/api/ready`'s `provisioning` check reads exactly this object.
    from types import SimpleNamespace

    from pi_gw_panel.net_control.netcheck import readiness_checks

    backlog = _backlog(provision.BACKLOG_WARN + 2)
    owed = {line.split()[1] for line in backlog}
    host = AddrHost(addrs=owed, refuse=owed)
    store = _store()
    store.set_setting("manage_segment", "0")
    store.set_setting("managed_segment_iface", "eth0.2")
    store.set_setting(provision.STALE_KEY, "\n".join(backlog))
    state = _State(store, LinuxBackend(host), _Dnsmasq())

    result = provision.host_provision(state)

    assert not result.ok
    assert f"{len(owed)} panel-owned segment addresses are awaiting removal" in result.error
    assert "by hand" in result.error                       # and what the operator has to do
    assert len(provision._parse_stale(store)) == len(owed)          # not one record dropped

    # `/api/ready` reads this same object, and the `provisioning` check is what carries it. Only
    # that one bit is asserted here; the rest of the gateway is stubbed out of the way.
    ready = readiness_checks(SimpleNamespace(
        store=store, settings=state.settings, net=state.net, dnsmasq=None,
        supervisor=SimpleNamespace(config_path="", status=lambda: {"running": False}),
        provision_result=state.provision_result))
    assert ready["provisioning"] is False


def test_a_backlog_under_the_threshold_is_not_announced_as_one():
    # Nothing behaves differently at the boundary except the sentence: the individual failures are
    # reported either way, and the count line is a diagnostic on top of them, not a state change.
    backlog = _backlog(provision.BACKLOG_WARN - 1)
    owed = {line.split()[1] for line in backlog}
    host = AddrHost(addrs=owed, refuse=owed)
    store = _store()
    store.set_setting("managed_segment_iface", "eth0.2")
    store.set_setting(provision.STALE_KEY, "\n".join(backlog))

    outcome = provision.reconcile_segment_addresses(store, _plan_with("192.168.10.2"), run=host)

    assert outcome.applied is True
    assert len(outcome.reasons) == len(owed)               # one per failed removal, and no more
    assert not any("awaiting removal" in reason for reason in outcome.reasons)

    # One more retained pair and the count appears, over the same unchanged behaviour.
    host.seed("eth0.2", ["192.168.99.2/24"])
    host.refuse.add("192.168.99.2/24")
    store.set_setting(provision.STALE_KEY,
                      "\n".join(backlog + ["eth0.2 192.168.99.2/24"]))
    outcome = provision.reconcile_segment_addresses(store, _plan_with("192.168.10.2"), run=host)

    assert outcome.applied is True
    assert len(outcome.reasons) == provision.BACKLOG_WARN + 1
    assert f"{provision.BACKLOG_WARN} panel-owned segment addresses" in outcome.reasons[-1]


def test_a_pass_with_a_large_backlog_still_retries_and_drains_every_entry():
    # The backlog is a retry list first. However long it is, the pass installs the desired address
    # and tries every recorded pair — which is what makes an operator's host-side cleanup enough.
    backlog = _backlog(provision.BACKLOG_WARN * 2)
    owed = {line.split()[1] for line in backlog}
    host = AddrHost(addrs=owed | {"192.168.10.2/24"})
    store = _store()
    store.set_setting("managed_segment_iface", "eth0.2")
    store.set_setting("managed_segment_addr4", "192.168.10.2/24")
    store.set_setting(provision.STALE_KEY, "\n".join(backlog))

    outcome = provision.reconcile_segment_addresses(store, _plan_with("192.168.10.2"), run=host)

    assert outcome.applied is True and outcome.reasons == []
    assert store.get_setting(provision.STALE_KEY) == ""
    assert host.addrs == {"192.168.10.2/24"}


def test_re_adopting_an_owned_address_with_a_large_backlog_keeps_it_on_the_interface():
    # The A -> B -> A property, at the size where the ceiling used to interfere with it. Coming back
    # to an address whose removal was refused must install it and must never then delete it, and no
    # amount of backlog may change that in either direction.
    backlog = _backlog(provision.BACKLOG_WARN * 2)
    owed = {line.split()[1] for line in backlog}
    readopted = min(owed)
    host = AddrHost(addrs=owed | {"192.168.9.2/24"}, refuse=owed | {"192.168.9.2/24"})
    store = _store()
    store.set_setting("managed_segment_iface", "eth0.2")
    store.set_setting("managed_segment_addr4", "192.168.9.2/24")
    store.set_setting(provision.STALE_KEY, "\n".join(backlog))

    outcome = provision.reconcile_segment_addresses(
        store, _plan_with(readopted.split("/")[0]), run=host)

    assert outcome.applied is True
    assert ["ip", "addr", "replace", readopted, "dev", "eth0.2"] in host.cmds()
    assert not any(cmd[:4] == ["ip", "addr", "del", readopted] for cmd in host.cmds())
    assert readopted in host.addrs                             # ON the interface, not deleted
    assert store.get_setting("managed_segment_addr4") == readopted
    assert ("eth0.2", readopted) not in provision._parse_stale(store)   # nor owed a removal


def test_the_clear_change_enable_cycle_applies_every_change_and_loses_no_record():
    # The cycle that grows the ledger fastest: a failed clear retains the current pairs AND erases
    # the current-address keys, so each round adds two more owned pairs. What must hold is not that
    # it stops — it does not, by design — but that every change still LANDS, that the record is a
    # faithful superset of what is on the host, and that one working pass drains all of it.
    host = RefusingAddrHost(addrs=["192.168.20.2/24", "fd00:1:2:20::1/64"])
    store = _store()
    store.set_setting("ipv6_enabled", "1")
    store.set_setting("managed_segment_iface", "eth0.2")
    store.set_setting("managed_segment_addr4", "192.168.20.2/24")
    store.set_setting("managed_segment_addr6", "fd00:1:2:20::1/64")
    state = _State(store, LinuxBackend(host), _Dnsmasq())

    for n in range(21, 21 + provision.BACKLOG_WARN):
        store.set_setting("manage_segment", "0")
        provision.host_provision(state)                 # clear: every removal refused
        store.set_setting("manage_segment", "1")
        store.set_setting("segment_ip", f"192.168.{n}.2")
        store.set_setting("segment_ip6", f"fd00:1:2:{n}::/64")
        provision.host_provision(state)                 # change, then enable again
        # Every round applies, however large the ledger has grown.
        assert f"192.168.{n}.2/24" in host.addrs
        assert store.get_setting("managed_segment_addr4") == f"192.168.{n}.2/24"

    stale = provision._parse_stale(store)
    assert len(stale) == len(set(stale))                    # no pair recorded twice
    assert _owned_set(store) >= {("eth0.2", a) for a in host.addrs}   # the record covers the host
    assert len(_owned_set(store)) >= provision.BACKLOG_WARN           # and it is genuinely large
    assert not state.provision_result.ok                             # which is reported, not hidden
    assert "awaiting removal" in state.provision_result.error

    # And it drains without hand-editing the store: once the host lets the removals through, the
    # very next pass clears the whole backlog.
    host.refusing = False
    host.refuse.clear()
    assert provision.host_provision(state).ok
    assert provision._parse_stale(store) == []
    last = 20 + provision.BACKLOG_WARN
    assert _owned_set(store) == {("eth0.2", f"192.168.{last}.2/24"),
                                 ("eth0.2", f"fd00:1:2:{last}::1/64")}


# --- a delegated prefix is recorded only once it is ON the interface ---------------------------
# The PD watcher used to persist `pd_segment_prefix6` BEFORE reconciling and put it back in the one
# branch that saw a non-applied outcome. An exception anywhere in between — a plan that will not
# build, a netlink socket that has gone — skipped that restore entirely, so a pass that reported
# failure left the new prefix recorded over a segment the host does not have. Resolving the
# candidate into the plan and persisting it only after `applied` is True needs no restore at all.


def _auto_pd_state(host):
    store = _store()
    store.set_setting("ipv6_enabled", "1")
    store.set_setting("segment_ip6", "auto")
    state = _State(store, LinuxBackend(host), _Dnsmasq())
    state.pd_client = _PD()
    assert provision.host_provision(state).ok
    return state


def _fill_backlog(store, host, count):
    backlog = _backlog(count)
    owed = {line.split()[1] for line in backlog}
    host.seed("eth0.2", owed)
    host.refuse |= owed
    store.set_setting(provision.STALE_KEY, "\n".join(backlog))
    return owed


class WatchingAddrHost(AddrHost):
    """Records what `pd_segment_prefix6` said at the instant a v6 address was installed.

    The ordering is the guarantee, not the end state: a prefix written before the `ip addr replace`
    that makes it true is a prefix every early exit leaves behind. `store` is attached by the test
    AFTER the initial bring-up, so only the callback's own install is sampled.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.store = None
        self.at_install = []

    def __call__(self, cmd, input=None):
        rest = _strip_flags(cmd)
        if self.store is not None and rest[:2] == ["addr", "replace"] and ":" in rest[2]:
            self.at_install.append(self.store.get_setting("pd_segment_prefix6") or "")
        return super().__call__(cmd, input)


class ExplodingReplaceHost(AddrHost):
    """An `AddrHost` whose v6 `ip addr replace` raises `OSError` once `exploding` is set.

    `OSError` is the one exception `reconcile_segment_addresses` deliberately does not catch (no
    `ip` binary at all, a dead netlink socket), so it travels out through the PD callback — which
    is exactly the path a capture-and-restore could not cover, the restore being a line the
    exception skips.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.exploding = False

    def __call__(self, cmd, input=None):
        rest = _strip_flags(cmd)
        if self.exploding and rest[:2] == ["addr", "replace"] and ":" in rest[2]:
            self.calls.append(cmd)
            raise OSError("netlink socket is gone")
        return super().__call__(cmd, input)


def test_the_pd_callback_records_the_prefix_only_after_it_is_on_the_interface():
    host = WatchingAddrHost()
    state = _auto_pd_state(host)
    store = state.store
    host.store = store
    applied = len(state.dnsmasq.applied)

    state.pd_client.callback("2001:db8:1200::/56")

    assert host.at_install == [""]      # not yet recorded when the kernel was told to install it
    assert store.get_setting("pd_segment_prefix6") == "2001:db8:1200:2::/64"   # recorded after
    assert store.get_setting("managed_segment_addr6") == "2001:db8:1200:2::1/64"
    assert len(state.dnsmasq.applied) == applied + 1
    assert state.provision_result.ok


def test_the_pd_callback_does_not_record_a_prefix_that_never_reached_the_interface():
    host = UninstallableAddrHost(reject=["2001:db8:1200:2::1/64"])
    state = _auto_pd_state(host)
    store = state.store
    ula = store.get_setting("managed_segment_addr6")
    applied = len(state.dnsmasq.applied)

    state.pd_client.callback("2001:db8:1200::/56")

    assert store.get_setting("pd_segment_prefix6") in (None, "")    # NOT recorded
    assert len(state.dnsmasq.applied) == applied                    # nothing keyed to the new plan
    assert host.addrs == {"192.168.10.2/24", ula}                   # the segment keeps its address
    assert not state.provision_result.ok and "not applied" in state.provision_result.error


def test_the_pd_callback_does_not_record_a_prefix_when_the_reconcile_raises():
    # The half a capture-and-restore missed. The first delegation lands and is recorded; the second
    # dies inside the reconcile, and the previous value survives because the new one was never
    # written — not because an `except` put it back, which is the line the exception skips.
    host = ExplodingReplaceHost()
    state = _auto_pd_state(host)
    store = state.store

    state.pd_client.callback("2001:db8:1200::/56")
    assert store.get_setting("pd_segment_prefix6") == "2001:db8:1200:2::/64"

    host.exploding = True
    with pytest.raises(OSError):
        state.pd_client.callback("2001:db8:aa00::/56")

    assert store.get_setting("pd_segment_prefix6") == "2001:db8:1200:2::/64"
    assert not state.provision_result.ok


def test_the_pd_callback_applies_over_a_large_backlog_instead_of_declining():
    # The caller with no operator behind it, and no request to fail. A long ledger may not strand a
    # delegation: the rotation lands, the backlog is retried with it, and the size is reported.
    host = AddrHost()
    state = _auto_pd_state(host)
    store = state.store
    owed = _fill_backlog(store, host, provision.BACKLOG_WARN * 2)
    host.refuse -= owed                     # it would go, if anything tried it
    applied = len(state.dnsmasq.applied)

    state.pd_client.callback("2001:db8:1200::/56")

    assert store.get_setting("pd_segment_prefix6") == "2001:db8:1200:2::/64"
    assert store.get_setting("managed_segment_addr6") == "2001:db8:1200:2::1/64"
    assert provision._parse_stale(store) == []            # the backlog went with it
    assert len(state.dnsmasq.applied) == applied + 1
    assert state.provision_result.ok




def test_ensure_nm_unmanaged_writes_conf_and_reloads():
    fake = FakeRun()
    written = {}
    provision.ensure_nm_unmanaged("eth0.2", run=fake,
                                  write_file=lambda p, t: written.update({p: t}),
                                  nm_active=lambda: True)
    assert provision.NM_CONF_PATH in written
    assert "unmanaged-devices=interface-name:eth0.2" in written[provision.NM_CONF_PATH]
    assert ["nsenter", "-t", "1", "-m", "-n", "--", "nmcli", "general", "reload"] in fake.cmds()


def test_ensure_nm_unmanaged_no_reload_when_nm_inactive():
    fake = FakeRun()
    provision.ensure_nm_unmanaged("eth0.2", run=fake, write_file=lambda p, t: None,
                                  nm_active=lambda: False)
    assert not any("nmcli" in c for c in fake.cmds())


def test_remove_nm_unmanaged_deletes_the_dropin_and_reloads():
    fake = FakeRun()
    removed = []
    provision.remove_nm_unmanaged(run=fake, remove_file=removed.append, nm_active=lambda: True)
    assert removed == [provision.NM_CONF_PATH]
    assert ["nsenter", "-t", "1", "-m", "-n", "--", "nmcli", "general", "reload"] in fake.cmds()


# --- Task 4: prefix resolution + orchestrator ----------------------------------

def test_ensure_segment_prefix6_generates_and_persists_ula():
    store = _store()
    store.set_setting("ipv6_enabled", "1")
    fixed = lambda n: bytes([1, 2, 3, 4, 5])[:n]
    p1 = provision.ensure_segment_prefix6(store, Settings(), rand=fixed)
    assert p1.startswith("fd") and p1.endswith(":2::/64")
    assert store.get_setting("segment_ip6") == p1
    assert provision.ensure_segment_prefix6(store, Settings(), rand=lambda n: bytes(n)) == p1


def test_ensure_segment_prefix6_keeps_static_and_skips_auto():
    store = _store()
    store.set_setting("ipv6_enabled", "1")
    store.set_setting("segment_ip6", "2001:db8:0:2::/64")
    assert provision.ensure_segment_prefix6(store, Settings()) == "2001:db8:0:2::/64"
    store.set_setting("segment_ip6", "auto")
    assert provision.ensure_segment_prefix6(store, Settings()) == "auto"


def test_auto_prefix_uses_persistent_ula_until_delegation_arrives():
    store = _store()
    store.set_setting("ipv6_enabled", "1")
    store.set_setting("segment_ip6", "auto")
    fixed = lambda n: bytes([1, 2, 3, 4, 5])[:n]

    first = provision.effective_segment_prefix6(store, Settings(), rand=fixed)
    second = provision.effective_segment_prefix6(store, Settings(), rand=lambda n: bytes(n))

    assert first == second == "fd01:203:405:2::/64"
    assert store.get_setting("segment_ip6") == "auto"
    assert store.get_setting("ula_prefix6") == first


def test_auto_prefix_prefers_delegated_segment_64():
    store = _store()
    store.set_setting("ipv6_enabled", "1")
    store.set_setting("segment_ip6", "auto")
    store.set_setting("pd_segment_prefix6", "2001:db8:1200:2::/64")
    assert provision.effective_segment_prefix6(store, Settings()) == "2001:db8:1200:2::/64"


def test_ensure_segment_prefix6_noop_when_v6_off():
    store = _store()
    assert provision.ensure_segment_prefix6(store, Settings()) == ""
    assert store.get_setting("segment_ip6") in (None, "")


def test_host_provision_runs_full_chain_on_linux():
    fake = FakeRun()
    store = _store()
    store.set_setting("manage_segment", "1")
    dnsmasq = _Dnsmasq()
    result = provision.host_provision(_State(store, LinuxBackend(fake), dnsmasq))
    cmds = fake.cmds()
    assert result.ok
    assert any(c[:2] == ["ip", "addr"] for c in cmds)
    assert dnsmasq.applied and "interface=eth0.2" in dnsmasq.applied[-1]


def test_host_provision_noop_on_dryrun_backend():
    store = _store()
    assert provision.host_provision(
        _State(store, DryRunBackend(), _Dnsmasq())).ok   # no `_run` -> skip


def test_host_provision_skipped_when_manage_segment_off():
    fake = FakeRun()
    store = _store()
    store.set_setting("manage_segment", "0")
    store.set_setting("managed_segment_iface", "eth0.2")
    store.set_setting("managed_segment_addr4", "192.168.10.2/24")
    store.set_setting("managed_segment_addr6", "fd00:1:2:3::1/64")
    dnsmasq, pd = _Dnsmasq(), _PD()
    state = _State(store, LinuxBackend(fake), dnsmasq)
    state.pd_client = pd
    result = provision.host_provision(state)
    assert result.ok
    assert pd.stopped == 1 and pd.cleared == 1 and dnsmasq.stopped == 1
    assert ["ip", "addr", "del", "192.168.10.2/24", "dev", "eth0.2"] in fake.cmds()
    assert ["ip", "-6", "addr", "del", "fd00:1:2:3::1/64", "dev", "eth0.2"] in fake.cmds()


def test_host_provision_hands_the_segment_back_when_manage_segment_off(monkeypatch):
    # Leaving the NM drop-in (and the VLAN the panel created) behind means NetworkManager
    # refuses to manage the interface forever after the panel stops owning it.
    fake = FakeRun()
    store = _store()
    store.set_setting("manage_segment", "0")
    store.set_setting("managed_segment_link", "eth0.2")
    removed: list[str] = []
    monkeypatch.setattr(provision, "_remove_file", removed.append)

    assert provision.host_provision(_State(store, LinuxBackend(fake), _Dnsmasq())).ok

    assert removed == [provision.NM_CONF_PATH]
    assert ["nsenter", "-t", "1", "-m", "-n", "--", "nmcli", "general", "reload"] in fake.cmds()
    assert ["ip", "link", "delete", "eth0.2"] in fake.cmds()
    assert store.get_setting("managed_segment_link") == ""


def test_host_provision_respects_manage_dnsmasq_off():
    fake = FakeRun()
    store = _store()
    store.set_setting("manage_dnsmasq", "0")
    dnsmasq = _Dnsmasq()
    provision.host_provision(_State(store, LinuxBackend(fake), dnsmasq))
    assert any(c[:2] == ["ip", "addr"] for c in fake.cmds())   # iface still provisioned
    assert dnsmasq.applied == []                               # but dnsmasq not started


# --- Task 11: PD client kicked off in auto mode --------------------------------

def test_host_provision_starts_pd_client_in_auto_mode():
    fake = FakeRun()
    store = _store()
    store.set_setting("manage_segment", "1")
    store.set_setting("ipv6_enabled", "1")
    store.set_setting("segment_ip6", "auto")
    state = _State(store, LinuxBackend(fake), _Dnsmasq())
    state.pd_client = _PD()
    result = provision.host_provision(state)
    assert result.ok
    assert state.pd_client.started == 1
    assert state.pd_client.callback is not None
    assert store.get_setting("managed_segment_addr6").startswith("fd")


def test_host_provision_stops_pd_and_clears_runtime_prefix_outside_auto_mode():
    fake = FakeRun()
    store = _store()
    store.set_setting("ipv6_enabled", "1")
    store.set_setting("segment_ip6", "fd00:1:2:3::/64")
    store.set_setting("pd_segment_prefix6", "2001:db8:1200:2::/64")
    state = _State(store, LinuxBackend(fake), _Dnsmasq())
    state.pd_client = _PD()

    assert provision.host_provision(state).ok
    assert state.pd_client.stopped == 1 and state.pd_client.cleared == 1
    assert store.get_setting("pd_segment_prefix6") in (None, "")


def test_pd_prefix_change_readdresses_segment_and_reapplies_dnsmasq():
    fake = FakeRun()
    store = _store()
    store.set_setting("ipv6_enabled", "1")
    store.set_setting("segment_ip6", "auto")
    state = _State(store, LinuxBackend(fake), _Dnsmasq())
    state.pd_client = _PD()
    assert provision.host_provision(state).ok
    old = store.get_setting("managed_segment_addr6")

    state.pd_client.callback("2001:db8:1200::/56")

    expected = "2001:db8:1200:2::1/64"
    assert store.get_setting("pd_segment_prefix6") == "2001:db8:1200:2::/64"
    assert store.get_setting("managed_segment_addr6") == expected
    assert ["ip", "-6", "addr", "replace", expected, "dev", "eth0.2"] in fake.cmds()
    assert ["ip", "-6", "addr", "del", old, "dev", "eth0.2"] in fake.cmds()
    assert len(state.dnsmasq.applied) == 2


def test_pd_callback_ignores_a_late_notification_after_manage_segment_off():
    # host_provision stops the PD client OUTSIDE the apply-lock (joining its watcher under the
    # lock the watcher itself needs would block for the whole timeout), so a notification can
    # still land right after the disable. It must not re-add the addresses just cleared.
    fake = FakeRun()
    store = _store()
    store.set_setting("ipv6_enabled", "1")
    store.set_setting("segment_ip6", "auto")
    state = _State(store, LinuxBackend(fake), _Dnsmasq())
    state.pd_client = _PD()
    assert provision.host_provision(state).ok
    callback = state.pd_client.callback
    applied = len(state.dnsmasq.applied)

    store.set_setting("manage_segment", "0")
    callback("2001:db8:1200::/56")

    assert store.get_setting("pd_segment_prefix6") in (None, "")
    assert len(state.dnsmasq.applied) == applied


def test_host_provision_reports_failure_as_net_result():
    store = _store()
    state = _State(store, LinuxBackend(lambda *_args, **_kwargs: (_ for _ in ()).throw(
        subprocess.CalledProcessError(1, ["ip"]))), _Dnsmasq())
    result = provision.host_provision(state)
    assert not result.ok
    assert "ip" in result.error
    assert state.provision_result is result


def test_pd_hook_atomically_reports_prefix_changes(tmp_path):
    script = tmp_path / "dhclient-pd-hook.sh"
    client = pd_client.PdClient("eth0", str(script), popen=lambda _cmd: None)

    client.write_hook()

    body = script.read_text()
    assert "new_ip6_prefix" in body and "new_ip6_prefixlen" in body
    assert "BOUND6" in body and "RENEW6" in body and "EXPIRE6" in body
    assert "mv \"${tmp}\"" in body
    assert script.stat().st_mode & 0o111


def test_delegated_prefix_helper_rejects_invalid_segment_indexes():
    assert pd_client.derive_segment_prefix("2001:db8:1200::/56", -1) is None
    assert pd_client.derive_segment_prefix("2001:db8:1200::/56", "2") is None
    assert pd_client.derive_segment_prefix("2001:db8:1200::/56", 65536) is None


def test_pd_prefix_file_notifies_only_on_change(tmp_path):
    seen = []
    client = pd_client.PdClient(
        "eth0", str(tmp_path / "hook.sh"), on_prefix_change=seen.append)
    prefix_file = tmp_path / "hook.sh.prefix"
    prefix_file.write_text("2001:db8:1200::/56\n")

    client.poll_once()
    client.poll_once()
    prefix_file.unlink()
    client.poll_once()

    assert seen == ["2001:db8:1200::/56", None]


def test_pd_clear_state_removes_stale_delegation_file(tmp_path):
    client = pd_client.PdClient("eth0", str(tmp_path / "hook.sh"))
    prefix_file = tmp_path / "hook.sh.prefix"
    prefix_file.write_text("2001:db8:1200::/56\n")
    client.clear_state()
    assert not prefix_file.exists()


# --- a link is retired only once the addresses that supersede it are ON the interface ----------
# The retarget is the one pass that touches BOTH halves of the segment, and it used to touch them
# in the wrong order: `ensure_segment_link` deleted the superseded VLAN, and only then were the
# addresses reconciled. Every way that reconcile can fail — a rejected `ip addr replace`, whatever
# the kernel's reason — therefore ended with the old link gone, its address gone with it, and the
# new link carrying nothing: no segment network at all, on a live gateway, with no previous
# configuration left for the `applied=False` gates below to preserve. (A ceiling that declined the
# change was once the trigger; removing the ceiling removed one trigger, not the defect.)
#
# Creation still goes first — the addresses need an interface to land on. Retirement is what moved:
# it happens after `applied`, so a failed retarget leaves the operator the working segment it
# started with, and leaves the ledger naming both links, which is what the host really has.


class LinkAndUninstallableAddrHost(LinkAndAddrHost, UninstallableAddrHost):
    """Both halves of the host in one runner, with `ip addr replace` refusable per address.

    `LinkAndAddrHost` models the links, `UninstallableAddrHost` the rejected replace; a retarget
    needs them together, because the question is what the LINKS look like after an address the
    kernel would not take.
    """


def _retarget_store(**extra) -> NodeStore:
    store = _store()
    store.set_setting("segment_iface", "eth0.9")             # retarget: eth0.2 -> eth0.9
    store.set_setting("segment_ip", "192.168.10.2")
    store.set_setting("managed_segment_iface", "eth0.2")
    store.set_setting("managed_segment_addr4", "192.168.9.2/24")
    store.set_setting(provision.LINK_KEY, "eth0.2")          # the panel created the old one
    for key, value in extra.items():
        store.set_setting(key, value)
    return store


def test_a_rejected_replace_during_a_retarget_leaves_the_old_link_and_its_address():
    # THE product failure. The kernel refuses the new segment address, so the retarget did not
    # happen — and the old VLAN, which is still the live one, must still be there carrying it.
    # Deleting it first leaves the gateway with one unaddressed link and no segment at all.
    host = LinkAndUninstallableAddrHost(links={"eth0", "eth0.2"}, addrs={"192.168.9.2/24"},
                                        reject=["192.168.10.2/24"])
    store = _retarget_store()
    state = _State(store, LinuxBackend(host), _Dnsmasq())

    result = provision.host_provision(state)

    assert "eth0.2" in host.links                       # the previous link SURVIVES
    assert host.addrs == {"192.168.9.2/24"}             # still carrying its address
    assert not any(cmd[:3] == ["ip", "link", "delete"] for cmd in host.cmds())
    assert host.links == {"eth0", "eth0.2", "eth0.9"}   # the new link is not the only one left
    assert not result.ok and "not applied" in result.error
    assert state.dnsmasq.applied == []                  # nothing keyed to a plan that did not land


def test_a_rejected_ipv6_replace_during_a_retarget_leaves_the_old_link_and_its_address():
    # The same, when only the SECOND replacement is refused: the IPv4 address landed on the new
    # link, so a pass could talk itself into calling the retarget done. It is not — the plan names
    # an IPv6 address the interface does not have — and the old link must still be intact.
    host = LinkAndUninstallableAddrHost(
        links={"eth0", "eth0.2"}, addrs={"192.168.9.2/24", "fd00:1:2:9::1/64"},
        reject=["fd00:1:2:3::1/64"])
    store = _retarget_store(ipv6_enabled="1", segment_ip6="fd00:1:2:3::/64")
    store.set_setting("managed_segment_addr6", "fd00:1:2:9::1/64")
    state = _State(store, LinuxBackend(host), _Dnsmasq())

    result = provision.host_provision(state)

    assert "eth0.2" in host.links                       # the previous link SURVIVES
    assert {"192.168.9.2/24", "fd00:1:2:9::1/64"} <= host.addrs
    assert not any(cmd[:3] == ["ip", "link", "delete"] for cmd in host.cmds())
    assert not result.ok and "not applied" in result.error
    assert state.dnsmasq.applied == []


def test_a_successful_retarget_still_retires_the_superseded_link():
    # The property the fix may not cost: once the addresses ARE on the new interface, the link
    # they superseded is genuinely superseded and goes, exactly as before.
    host = LinkAndAddrHost(links={"eth0", "eth0.2"}, addrs={"192.168.9.2/24"})
    store = _retarget_store()
    state = _State(store, LinuxBackend(host), _Dnsmasq())

    result = provision.host_provision(state)

    assert result.ok
    assert host.links == {"eth0", "eth0.9"}                     # the old VLAN is gone
    assert ["ip", "link", "delete", "eth0.2"] in host.cmds()
    assert provision._parse_links(store) == ["eth0.9"]
    assert host.addrs == {"192.168.10.2/24"}
    assert len(state.dnsmasq.applied) == 1 and "192.168.10.2" in state.dnsmasq.applied[0]


def test_the_link_ledger_after_a_failed_retarget_still_describes_the_host():
    # What the panel CLAIMS has to match what is on the wire, or the next pass acts on fiction.
    # Both VLANs are on the host and both were created by this panel, so both stay recorded — and
    # the moment the addresses land, the entry that is now genuinely superseded drains.
    host = LinkAndUninstallableAddrHost(links={"eth0", "eth0.2"}, addrs={"192.168.9.2/24"},
                                        reject=["192.168.10.2/24"])
    store = _retarget_store()
    state = _State(store, LinuxBackend(host), _Dnsmasq())

    assert not provision.host_provision(state).ok

    assert provision._parse_links(store) == ["eth0.2", "eth0.9"]
    assert set(provision._parse_links(store)) == host.links - {"eth0"}   # the ledger IS the host
    # ...and the address the old link still carries is still recorded as owed a removal.
    assert ("eth0.2", "192.168.9.2/24") in provision._parse_stale(store)

    # The next pass, on a host that now accepts the address, finishes the retarget it started.
    host.reject.clear()
    assert provision.host_provision(state).ok
    assert host.links == {"eth0", "eth0.9"} and provision._parse_links(store) == ["eth0.9"]
    assert host.addrs == {"192.168.10.2/24"}


def test_a_refused_ipv6_replacement_names_the_ipv6_address_and_not_the_ipv4_one():
    # Both replacements ran under one `try` that blamed `new4` whatever failed, so this reported a
    # working IPv4 address as the failure and never printed the IPv6 target the kernel rejected.
    host = UninstallableAddrHost(reject=["fd00:1:2:3::1/64"])
    store = _store()

    outcome = provision.reconcile_segment_addresses(
        store, _plan_with("192.168.10.2", "fd00:1:2:3::/64"), run=host)

    assert outcome.applied is False
    assert len(outcome.reasons) == 1
    assert outcome.reasons[0].startswith("fd00:1:2:3::1/64 on eth0.2: ")
    assert "192.168.10.2/24" not in outcome.reasons[0]      # the address that WORKED is not blamed


def test_a_refused_ipv4_replacement_still_names_the_ipv4_address():
    # The other direction, so the attribution is per command and not a swapped constant.
    host = UninstallableAddrHost(reject=["192.168.10.2/24"])
    store = _store()

    outcome = provision.reconcile_segment_addresses(
        store, _plan_with("192.168.10.2", "fd00:1:2:3::/64"), run=host)

    assert outcome.applied is False
    assert outcome.reasons[0].startswith("192.168.10.2/24 on eth0.2: ")
    assert "fd00:1:2:3::1/64" not in outcome.reasons[0]


def test_a_delegated_prefix_the_kernel_rejects_is_named_in_the_failure():
    # Where the misattribution hurt most: a delegated /64 the kernel will not take is diagnosed by
    # the address the kernel named. The PD callback is also the caller with no request to fail, so
    # this result is the only place an operator can read which address was actually refused.
    host = UninstallableAddrHost(reject=["2001:db8:1200:2::1/64"])
    state = _auto_pd_state(host)

    state.pd_client.callback("2001:db8:1200::/56")

    error = state.provision_result.error
    assert "not applied" in error
    assert "2001:db8:1200:2::1/64" in error            # the rejected delegated address
    assert "192.168.10.2/24" not in error              # not the IPv4 one, which is on the interface


# --- a candidate link is raised only once every address it must carry is ON it -----------------
# The other half of the retarget ordering, and the half that is not merely an availability bug.
# `ensure_segment_link` used to bring the candidate UP the moment it had created it, before a
# single address was reconciled. The replacements are sequential, so a pass that installed IPv4
# and was then refused IPv6 — or was killed between the two — left an interface that was up and
# addressed while dnsmasq, the NetworkManager drop-in, the kill-switch drop and the tproxy
# redirect were all still scoped, by interface name, to the interface it replaces. That is not a
# failed apply: it is a live interface outside every rule the panel enforces, and it stays one
# until someone notices. `/api/ready` reporting the pass as failed does not change what the host
# is carrying.
#
# So the bring-up is `activate_segment_link`, run after `applied` and immediately before the
# retirement, and there is deliberately no `ip link set … down` to match it: down is where a
# created candidate already is, and a candidate the panel did NOT create may be the operator's own
# interface, already up, already carrying the address this gateway is reached on.


def _retarget_host(old6="", cls=None, **kwargs):
    """A live gateway mid-retarget: `eth0.2` is up carrying the segment address, `eth0.9` is not
    on the host yet. Both halves are modelled per interface, including which links are UP."""
    on = {"eth0": {"192.168.1.20/24"},
          "eth0.2": {"192.168.9.2/24"} | ({old6} if old6 else set())}
    return (cls or LinkAndUninstallableAddrHost)(
        links={"eth0", "eth0.2"}, up={"eth0", "eth0.2"}, on=on, **kwargs)


def _assert_old_segment_untouched(host, state):
    """The interface every rule still names is exactly as the pass found it."""
    assert "eth0.2" in host.links
    assert host.is_up("eth0.2")
    assert "192.168.9.2/24" in host.addrs_on("eth0.2")
    assert not any(cmd[:3] == ["ip", "link", "delete"] for cmd in host.cmds())
    assert state.dnsmasq.applied == []                       # dnsmasq still serves the old one
    assert not any("nsenter" in cmd for cmd in host.cmds())   # and NM was not repointed either


def test_a_candidate_whose_ipv6_replace_is_refused_is_left_down():
    # THE finding. IPv4 landed on the new VLAN and IPv6 did not, so the plan is not on the host —
    # and the half-addressed candidate must not be carrying traffic outside the policy while the
    # operator works out why. It was created down; nothing raises it.
    host = _retarget_host(old6="fd00:1:2:9::1/64", reject=["fd00:1:2:3::1/64"])
    store = _retarget_store(ipv6_enabled="1", segment_ip6="fd00:1:2:3::/64")
    store.set_setting("managed_segment_addr6", "fd00:1:2:9::1/64")
    state = _State(store, LinuxBackend(host), _Dnsmasq())

    result = provision.host_provision(state)

    assert "eth0.9" in host.links                             # created — the address needed it
    assert host.addrs_on("eth0.9") == {"192.168.10.2/24"}     # and the IPv4 replacement did land
    assert not host.is_up("eth0.9")                           # ...but it is DOWN, so it carries none
    assert not any(cmd[:3] == ["ip", "link", "set"] for cmd in host.cmds())
    _assert_old_segment_untouched(host, state)
    assert not result.ok and "not applied" in result.error


def test_a_candidate_whose_ipv4_replace_is_refused_is_left_down():
    # The first replacement refused: nothing landed at all, and the candidate is an empty, down
    # interface. Same rule, so that "down" can never be a property of which command failed.
    host = _retarget_host(reject=["192.168.10.2/24"])
    store = _retarget_store()
    state = _State(store, LinuxBackend(host), _Dnsmasq())

    result = provision.host_provision(state)

    assert "eth0.9" in host.links and host.addrs_on("eth0.9") == set()
    assert not host.is_up("eth0.9")
    assert not any(cmd[:3] == ["ip", "link", "set"] for cmd in host.cmds())
    _assert_old_segment_untouched(host, state)
    assert not result.ok and "not applied" in result.error


class _KilledBetweenReplacements(LinkAndAddrHost):
    """A host whose panel process is KILLED between the IPv4 and IPv6 replacements.

    `KeyboardInterrupt` is not an `Exception`, so it walks straight out through `host_provision`'s
    handler, its logging and its result and out of the call — which is the point. A real SIGKILL
    runs no cleanup either, so whatever the candidate is left in has to be what the ORDER of the
    commands already put it in, and not something a failure path was kind enough to repair.
    """

    def __call__(self, cmd, input=None):
        rest = _strip_flags(cmd)
        if rest[:2] == ["addr", "replace"] and ":" in rest[2]:
            self.calls.append(cmd)
            raise KeyboardInterrupt("killed between the two replacements")
        return super().__call__(cmd, input)


def test_a_process_killed_between_the_two_replacements_leaves_the_candidate_down():
    host = _retarget_host(old6="fd00:1:2:9::1/64", cls=_KilledBetweenReplacements)
    store = _retarget_store(ipv6_enabled="1", segment_ip6="fd00:1:2:3::/64")
    store.set_setting("managed_segment_addr6", "fd00:1:2:9::1/64")
    state = _State(store, LinuxBackend(host), _Dnsmasq())

    with pytest.raises(KeyboardInterrupt):      # nothing catches it: no cleanup runs, by design
        provision.host_provision(state)

    assert host.addrs_on("eth0.9") == {"192.168.10.2/24"}    # IPv4 on it, IPv6 never issued
    assert not host.is_up("eth0.9")
    assert not any(cmd[:3] == ["ip", "link", "set"] for cmd in host.cmds())
    _assert_old_segment_untouched(host, state)


def test_a_successful_retarget_raises_the_candidate_after_the_addresses_and_before_the_retirement():
    # The ordering the fix has to keep, stated as the sequence itself: every replacement the plan
    # asks for, THEN the bring-up, THEN the retirement of what it supersedes. The middle step is
    # the one that moved; the outer two are the property the previous fix established.
    host = _retarget_host(old6="fd00:1:2:9::1/64")
    store = _retarget_store(ipv6_enabled="1", segment_ip6="fd00:1:2:3::/64")
    store.set_setting("managed_segment_addr6", "fd00:1:2:9::1/64")
    state = _State(store, LinuxBackend(host), _Dnsmasq())

    result = provision.host_provision(state)

    cmds = host.cmds()
    up = cmds.index(["ip", "link", "set", "eth0.9", "up"])
    assert cmds.index(["ip", "addr", "replace", "192.168.10.2/24", "dev", "eth0.9"]) < up
    assert cmds.index(["ip", "-6", "addr", "replace", "fd00:1:2:3::1/64", "dev", "eth0.9"]) < up
    assert up < cmds.index(["ip", "link", "delete", "eth0.2"])

    assert result.ok
    assert host.is_up("eth0.9")
    assert host.addrs_on("eth0.9") == {"192.168.10.2/24", "fd00:1:2:3::1/64"}
    assert "eth0.2" not in host.links and provision._parse_links(store) == ["eth0.9"]
    assert len(state.dnsmasq.applied) == 1 and "192.168.10.2" in state.dnsmasq.applied[0]


def test_a_candidate_that_was_already_up_is_never_taken_down_by_a_failed_pass():
    # The counterpart, and the reason the failure path has no `ip link set … down` in it. Here
    # `eth0.9` is the operator's own interface — on the host before the pass, already up, already
    # carrying an address the panel did not install — and the retarget onto it is refused. The
    # panel may not raise it (it cannot say the segment works) and may not lower it either: it
    # never created it, so this may be the interface the gateway is being reached on. Left as found.
    host = LinkAndUninstallableAddrHost(
        links={"eth0", "eth0.2", "eth0.9"}, up={"eth0", "eth0.2", "eth0.9"},
        on={"eth0.2": {"192.168.9.2/24"}, "eth0.9": {"10.9.9.1/24"}},
        reject=["192.168.10.2/24"])
    store = _retarget_store()
    state = _State(store, LinuxBackend(host), _Dnsmasq())

    result = provision.host_provision(state)

    assert not any(cmd[:3] == ["ip", "link", "add"] for cmd in host.cmds())   # not the panel's
    assert provision._parse_links(store) == ["eth0.2"]                       # never claimed
    assert host.is_up("eth0.9")                                              # STILL up
    assert host.addrs_on("eth0.9") == {"10.9.9.1/24"}        # and carrying only what it had
    assert not any(cmd[:3] == ["ip", "link", "set"] for cmd in host.cmds())
    _assert_old_segment_untouched(host, state)
    assert not result.ok and "not applied" in result.error


def test_a_pass_over_the_live_segment_that_fails_never_lowers_it():
    # No retarget at all: the segment is `eth0.2`, it is up and working, and only the IPv6 address
    # this pass adds is refused. Bringing the interface the whole gateway is on down over that
    # would turn a rejected address into an outage, so the pass leaves it exactly as it is.
    host = LinkAndUninstallableAddrHost(
        links={"eth0", "eth0.2"}, up={"eth0", "eth0.2"}, on={"eth0.2": {"192.168.9.2/24"}},
        reject=["fd00:1:2:3::1/64"])
    store = _store()
    store.set_setting("segment_ip", "192.168.9.2")
    store.set_setting("ipv6_enabled", "1")
    store.set_setting("segment_ip6", "fd00:1:2:3::/64")
    store.set_setting("managed_segment_iface", "eth0.2")
    store.set_setting("managed_segment_addr4", "192.168.9.2/24")
    state = _State(store, LinuxBackend(host), _Dnsmasq())

    result = provision.host_provision(state)

    assert host.is_up("eth0.2") and host.addrs_on("eth0.2") == {"192.168.9.2/24"}
    assert not any(cmd[:3] == ["ip", "link", "set"] for cmd in host.cmds())
    assert not result.ok and "not applied" in result.error
    assert state.dnsmasq.applied == []


# --- the segment is moved under a ruleset that covers BOTH interfaces --------------------------
# Retargeting the segment spans a VLAN link, two address replacements, NetworkManager, dnsmasq and
# the interface-scoped nft/tproxy enforcement, and none of it is a transaction. What made that
# dangerous rather than merely untidy is that the enforcement was not part of the sequence at all:
# the pass raised the candidate and the ruleset was re-rendered afterwards, by the caller, from a
# store that names exactly ONE segment interface. Every ordering of that leaves a window in which
# an interface is up and addressed while the kill-switch drop and the tproxy redirect name the
# other one — a path around the segment's own policy, lasting as long as the window does.
#
# So the enforcement is staged first, over both interfaces at once, and narrowed back only once
# the superseded link is proven gone. A transitional ruleset is a superset: every intermediate
# state is over-covered, never under-covered, and that is the property these tests check — at
# every boundary of the sequence, including the ones a killed process stops at.

_SEGMENT_ADDRS = {"192.168.9.2/24", "192.168.10.2/24", "fd00:1:2:9::1/64", "fd00:1:2:3::1/64"}


def _reachable_segment(host) -> set:
    """Interfaces a segment client can put a packet onto: UP, and carrying a segment address."""
    return {iface for iface in host.on
            if host.is_up(iface) and host.addrs_on(iface) & _SEGMENT_ADDRS}


def _live_ruleset(state) -> str:
    """The ruleset the host is enforcing right now — the last one that finished loading."""
    return state.net.applied[-1]


def _assert_segment_covered(host, state) -> None:
    """THE INVARIANT: nothing carrying the segment is outside the ruleset in force."""
    outside = _reachable_segment(host) - _enforced_ifaces(_live_ruleset(state))
    assert not outside, (f"{outside} carry the segment and the live ruleset does not cover them: "
                         f"{_enforced_ifaces(_live_ruleset(state))}")


class _KillAt(LinkAndUninstallableAddrHost):
    """A host whose panel process is KILLED at a chosen command.

    `KeyboardInterrupt` is not an `Exception`, so it walks straight out through `host_provision`'s
    handler, its logging and its result — which is the point. A real SIGKILL runs no cleanup
    either, so whatever the host is left in has to be what the ORDER of the steps already put it
    in, and not something a failure path was kind enough to repair.
    """

    def __init__(self, kill=None, **kwargs):
        super().__init__(**kwargs)
        self.kill = kill

    def __call__(self, cmd, input=None):
        if self.kill is not None and self.kill(cmd):
            self.calls.append(cmd)
            raise KeyboardInterrupt(f"killed at: {' '.join(cmd)}")
        return super().__call__(cmd, input)


def _at(*prefix):
    return lambda cmd: cmd[:len(prefix)] == list(prefix)


def _seed_enforcement(state) -> None:
    """The ruleset a live gateway is already carrying: the interface it is being moved OFF."""
    old = NetPlan.from_store(state.store, Settings())
    old.segment_iface, old.segment_ip = "eth0.2", "192.168.9.2"
    assert state.net.apply_guard(old).ok
    assert _enforced_ifaces(_live_ruleset(state)) == {"eth0.2"}


def _run_move(kill_cmd=None, kill_apply=None, kill_dnsmasq=False, stubborn=False, fail=None,
              running=False, owned_old=True):
    """A live gateway moving its segment from eth0.2 to eth0.9, interrupted where asked.

    `stubborn` refuses the removal of the OLD addresses, which is the case where the interface
    being left really does keep carrying the segment for the whole move rather than losing its
    address at the replacement — the state that makes covering both more than a formality.
    `running` is the tunnel: up, the enforcement is the full tproxy ruleset; down, the fail-closed
    guard. Both are scoped by the same interface names, and the move has to hold for either.
    """
    old = {("eth0.2", "192.168.9.2/24"), ("eth0.2", "fd00:1:2:9::1/64")} if stubborn else set()
    host = _KillAt(kill=kill_cmd, links={"eth0", "eth0.2"}, up={"eth0", "eth0.2"},
                   on={"eth0": {"192.168.1.20/24"},
                       "eth0.2": {"192.168.9.2/24", "fd00:1:2:9::1/64"}},
                   refuse=old)
    store = _retarget_store(ipv6_enabled="1", segment_ip6="fd00:1:2:3::/64",
                            **({} if owned_old else {provision.LINK_KEY: ""}))
    store.set_setting("managed_segment_addr6", "fd00:1:2:9::1/64")
    if running:
        store.set_setting("active_node_id", "1")
    state = _State(store, LinuxBackend(host), _Dnsmasq(kill=kill_dnsmasq), running=running)
    _seed_enforcement(state)                    # ...before the backend is told to start failing
    state.net.fail, state.net.kill_in = fail, kill_apply
    killed, result = False, None
    try:
        result = provision.host_provision(state)
    except KeyboardInterrupt:
        killed = True
    return host, state, result, killed


# Every boundary between two steps of the move, named by the step that has just finished. The
# enumeration is the test: a kill at any of them must leave either the old configuration intact or
# a fully covered new one.
_BOUNDARIES = [
    ("1 the candidate link is created", {"kill_apply": 0}),
    ("2 the enforcement covers both", {"kill_cmd": _at("ip", "addr", "replace")}),
    ("3a the IPv4 address is replaced", {"kill_cmd": _at("ip", "-6", "addr", "replace")}),
    ("3b both addresses are replaced", {"kill_cmd": _at("nsenter")}),
    ("4 NetworkManager is told to keep off", {"kill_cmd": _at("ip", "link", "set")}),
    ("5 the candidate is raised", {"kill_dnsmasq": True}),
    ("6a dnsmasq serves the new segment", {"kill_cmd": _at("ip", "link", "delete")}),
    ("6b the superseded link is retired", {"kill_apply": 1}),
    ("7 nothing — the move completes", {}),
]


@pytest.mark.parametrize("stubborn", [False, True], ids=["old-address-released", "old-address-kept"])
@pytest.mark.parametrize("label, where", _BOUNDARIES, ids=[b[0][0] for b in _BOUNDARIES])
def test_a_kill_at_every_boundary_of_a_segment_move_leaves_a_covered_host(label, where, stubborn):
    host, state, _result, killed = _run_move(stubborn=stubborn, **where)

    _assert_segment_covered(host, state)
    # ...and the two halves of the move that a client can actually notice never get ahead of it:
    # nothing is served on an interface that is not up, and nothing is retired before the segment
    # it carried has been re-served somewhere else.
    if state.dnsmasq.applied:
        assert host.is_up("eth0.9") and "192.168.10.2/24" in host.addrs_on("eth0.9")
    if "eth0.2" not in host.links:
        assert state.dnsmasq.applied and "192.168.10.2" in state.dnsmasq.applied[-1]
    # The kill lands wherever the sequence actually reaches. The one boundary a stubborn old
    # address never reaches is the narrowing: with the segment still on the interface being left,
    # that step is deliberately not taken, and the transitional ruleset is the final one.
    reached = label != "7 nothing — the move completes" and not (
        stubborn and label.startswith("6b"))
    assert killed is reached
    if not killed:
        assert _enforced_ifaces(_live_ruleset(state)) == (
            {"eth0.9", "eth0.2"} if stubborn else {"eth0.9"})


def test_the_transitional_ruleset_names_both_interfaces_and_the_final_one_names_only_the_new():
    # The rendered rules, not a flag: what nft is actually given is an interface SET while the
    # move is in flight, so the kill-switch drop and the tproxy redirect apply to the interface
    # being left and the one being moved to at the same time. Run with the tunnel UP, which is the
    # mode that renders both of those rules.
    _host, state, result, killed = _run_move(running=True)

    assert result.ok and not killed
    staged, final = state.net.applied[1], state.net.applied[-1]
    both = 'iifname { "eth0.9", "eth0.2" }'
    assert _enforced_ifaces(staged) == {"eth0.9", "eth0.2"}
    assert f"{both} ip daddr != {{ 127.0.0.0/8" in staged            # the kill-switch drop
    assert f"{both} meta l4proto {{ tcp, udp }}" in staged           # the v4 tproxy redirect
    assert f"{both} ip daddr {{ 192.168.10.0/24" in staged           # the DHCP carve-out
    assert f"{both} ip6 daddr !=" in staged                          # the v6 drop
    assert f"{both} meta l4proto {{ tcp, udp }} meta mark set 0x40 tproxy ip6 to :52346" in staged
    assert _enforced_ifaces(final) == {"eth0.9"} and 'iifname "eth0.9"' in final
    assert "{" not in final.split("iifname ")[1][:12]                # one name, not a set


def test_a_completed_move_ends_on_the_new_interface_with_nothing_left_naming_the_old():
    host, state, result, _killed = _run_move()

    assert result.ok
    assert host.is_up("eth0.9")
    assert host.addrs_on("eth0.9") == {"192.168.10.2/24", "fd00:1:2:3::1/64"}
    assert "eth0.2" not in host.links and provision._parse_links(state.store) == ["eth0.9"]
    assert len(state.dnsmasq.applied) == 1 and "192.168.10.2" in state.dnsmasq.applied[0]
    assert _enforced_ifaces(_live_ruleset(state)) == {"eth0.9"}
    # The order itself: NetworkManager is told to keep off the candidate BEFORE it is raised (so
    # NM cannot reconfigure an interface the panel has just addressed), the bring-up is the commit
    # point, and the superseded link is retired after it — never before.
    cmds = host.cmds()
    nm = next(i for i, cmd in enumerate(cmds) if cmd[:1] == ["nsenter"])
    up = cmds.index(["ip", "link", "set", "eth0.9", "up"])
    assert nm < up < cmds.index(["ip", "link", "delete", "eth0.2"])


def test_the_drop_in_covers_both_interfaces_while_the_old_one_is_still_there():
    written = []
    provision.ensure_nm_unmanaged("eth0.9", run=FakeRun(), nm_active=lambda: False,
                                  write_file=lambda p, t: written.append(t), also=["eth0.2"])
    provision.ensure_nm_unmanaged("eth0.9", run=FakeRun(), nm_active=lambda: False,
                                  write_file=lambda p, t: written.append(t))

    assert "unmanaged-devices=interface-name:eth0.9;interface-name:eth0.2" in written[0]
    assert "unmanaged-devices=interface-name:eth0.9\n" in written[1]


def test_an_enforcement_that_cannot_be_staged_stops_the_move_before_the_first_replacement():
    # The fail-closed direction of the same rule. If the ruleset that would cover the candidate
    # cannot be installed, the pass may not go on to the step that makes the candidate reachable —
    # so nothing is replaced, nothing is raised, nothing is retired, and the gateway is left on the
    # configuration whose enforcement is still in force.
    host, state, result, _killed = _run_move(fail="nft: Operation not permitted")

    assert not any(cmd[:3] == ["ip", "addr", "replace"] for cmd in host.cmds())
    assert not any(cmd[:3] == ["ip", "link", "set"] for cmd in host.cmds())
    assert not any(cmd[:3] == ["ip", "link", "delete"] for cmd in host.cmds())
    assert host.addrs_on("eth0.2") == {"192.168.9.2/24", "fd00:1:2:9::1/64"}
    assert state.dnsmasq.applied == []
    assert not result.ok and "could not be installed" in result.error
    assert "not applied" in result.error            # the addresses are not on the interface
    _assert_segment_covered(host, state)


def test_a_candidate_that_is_already_up_is_covered_before_its_first_address_lands():
    # THE already-up case. `eth0.9` is the operator's own interface — on the host before the pass
    # and already up — so the first `ip addr replace` is what makes it reachable ON THE SEGMENT,
    # long before any bring-up this module could gate. The IPv6 replacement is then refused, so the
    # move fails half-done: the panel may not raise the candidate (it cannot say the segment
    # works), may not lower it (it never created it, and it may be the interface this gateway is
    # reached on) — and so the ruleset must already have covered it before the IPv4 address went on.
    host = LinkAndUninstallableAddrHost(
        links={"eth0", "eth0.2", "eth0.9"}, up={"eth0", "eth0.2", "eth0.9"},
        on={"eth0.2": {"192.168.9.2/24", "fd00:1:2:9::1/64"}, "eth0.9": {"10.9.9.1/24"}},
        reject=["fd00:1:2:3::1/64"])
    store = _retarget_store(ipv6_enabled="1", segment_ip6="fd00:1:2:3::/64")
    store.set_setting("managed_segment_addr6", "fd00:1:2:9::1/64")
    state = _State(store, LinuxBackend(host), _Dnsmasq())
    _seed_enforcement(state)

    result = provision.host_provision(state)

    assert not any(cmd[:3] == ["ip", "link", "add"] for cmd in host.cmds())     # not the panel's
    assert host.is_up("eth0.9")                                                # STILL up...
    assert not any(cmd[:3] == ["ip", "link", "set"] for cmd in host.cmds())    # ...and never lowered
    assert host.addrs_on("eth0.9") == {"10.9.9.1/24", "192.168.10.2/24"}       # IPv4 landed on it
    assert _enforced_ifaces(_live_ruleset(state)) == {"eth0.9", "eth0.2"}      # both covered
    _assert_segment_covered(host, state)
    assert not result.ok and "not applied" in result.error
    assert state.dnsmasq.applied == []                     # and nothing keyed to a half-done move


def test_a_delegation_renewing_onto_a_candidate_a_failed_move_left_down_does_not_serve_it():
    # The other way an interface could be made operational outside the sequence. The PD watcher
    # runs on its own thread, long after the pass that started it, and a renewal can land on a
    # segment an earlier FAILED move left created and DOWN. It used to raise that interface and
    # then apply dnsmasq to it — completing none of the rest of the move (the superseded link, the
    # drop-in, the interface-scoped ruleset) and making the bypass operational.
    host = LinkAndUninstallableAddrHost(links={"eth0", "eth0.2"}, up={"eth0", "eth0.2"},
                                        on={"eth0.2": {"192.168.10.2/24"}})
    state = _auto_pd_state(host)                    # a working segment on eth0.2, PD watching it
    store = state.store
    _seed_enforcement(state)

    store.set_setting("segment_iface", "eth0.9")    # ...and a move onto a link the kernel refuses
    host.reject.add(("eth0.9", "192.168.10.2/24"))
    assert not provision.host_provision(state).ok
    assert "eth0.9" in host.links and not host.is_up("eth0.9")
    host.reject.clear()      # the kernel would take the addresses now; the MOVE is what is missing
    served = len(state.dnsmasq.applied)

    state.pd_client.callback("2001:db8:1200::/56")

    assert len(state.dnsmasq.applied) == served               # the segment is NOT served on it
    assert not host.is_up("eth0.9")                           # nor was it raised to serve it
    assert not any(cmd[:4] == ["ip", "link", "set", "eth0.9"] for cmd in host.cmds())
    assert not state.provision_result.ok
    assert "is not up" in state.provision_result.error
    assert "full provisioning pass" in state.provision_result.error
    _assert_segment_covered(host, state)


def test_a_delegation_renewing_onto_a_segment_that_is_up_is_still_served():
    # The property that answer may not cost: the ordinary renewal, on an interface the pass raised,
    # still reconfigures dnsmasq. "Not up" has to mean the host said so, not "the panel stopped
    # asking".
    host = AddrHost()
    state = _auto_pd_state(host)
    served = len(state.dnsmasq.applied)

    state.pd_client.callback("2001:db8:1200::/56")

    assert host.is_up("eth0.2")
    assert len(state.dnsmasq.applied) == served + 1
    assert state.provision_result.ok


def test_the_drop_in_keeps_the_old_interface_unmanaged_until_the_move_is_complete(monkeypatch):
    # The call site, not just the helper: NetworkManager is told to keep off BOTH interfaces
    # before the candidate is raised, so it cannot reconfigure the one still in service, and is
    # narrowed to the new interface alone once the old link is gone.
    written = []
    monkeypatch.setattr(provision, "_write_file", lambda path, text: written.append(text))

    *_, result, _killed = _run_move()

    assert result.ok
    assert "unmanaged-devices=interface-name:eth0.9;interface-name:eth0.2" in written[0]
    assert written[-1].endswith("unmanaged-devices=interface-name:eth0.9\n")


def test_the_dry_run_backend_renders_a_transitional_ruleset_exactly_as_the_host_one_would():
    # The dev/CI backend records what it would install, and a move is the one time that render is
    # not the store's own plan. It has no host to stage anything on — `host_provision` returns
    # before any of this on a backend with no `_run` seam — but what it reports must never be
    # narrower than what the real backend loads.
    plan = _plan_for("eth0.9")
    plan.kill_switch = True
    result = DryRunBackend().apply_tproxy(provision.covering_plan(plan, ["eth0.2"]))

    assert _enforced_ifaces(result.rendered) == {"eth0.9", "eth0.2"}
    assert 'iifname { "eth0.9", "eth0.2" }' in result.rendered


def test_an_interface_the_panel_never_created_keeps_its_cover_until_its_address_is_off_it():
    # Narrowing is what ends the cover, and the interface being left is not always the panel's to
    # delete: `eth0.2` here is the operator's own, so no retirement will ever remove it, and the
    # only thing that takes it out of service is the removal of the segment address. That removal
    # is refused — so the interface is still up, still carrying the segment, and the ruleset must
    # still name it when the pass ends, however completely the new interface came up.
    host, state, result, _killed = _run_move(stubborn=True, owned_old=False)

    assert host.is_up("eth0.9") and "192.168.10.2/24" in host.addrs_on("eth0.9")   # the move landed
    assert "eth0.2" in host.links and host.is_up("eth0.2")                         # ...and so is it
    assert "192.168.9.2/24" in host.addrs_on("eth0.2")
    assert _enforced_ifaces(_live_ruleset(state)) == {"eth0.9", "eth0.2"}
    _assert_segment_covered(host, state)
    assert ("eth0.2", "192.168.9.2/24") in provision._parse_stale(state.store)   # and it is owed
    assert not result.ok and "not removed" in result.error


def test_the_cover_narrows_as_soon_as_the_old_interface_is_out_of_service():
    # The other half, so "keep covering it" cannot quietly become "never narrow": the same
    # operator-owned interface, with the removal accepted. The address comes off, nothing is left
    # carrying the old segment, and the ruleset narrows to the configured interface alone.
    host, state, result, _killed = _run_move(owned_old=False)

    assert result.ok
    assert "eth0.2" in host.links and host.addrs_on("eth0.2") == set()
    assert _enforced_ifaces(_live_ruleset(state)) == {"eth0.9"}
    assert provision._parse_stale(state.store) == []


# --- the cover is DURABLE: what may be up outlives the pass that put it there -------------------
# Staging a transitional ruleset covers a move while the pass performing it is running. It cannot
# cover what the pass LEAVES BEHIND, and a move that fails leaves exactly that: the ownership record
# was rewritten to name the candidate before the kernel was touched, so a refused `ip addr replace`
# ends with the old interface up, still carrying the segment address, and named by nothing but a
# stale pair. Every LATER render — the next `sync_net`, the next boot's guard — is built from the
# store, and one built from the segment interface alone narrows onto the candidate while the old
# interface is still live. That is the direct-WAN bypass, and it lasts as long as the interface does.
#
# So what may be up is written down and every store-derived render consumes it. The interface that
# matters most here is the operator's OWN: a panel-created VLAN is at least in the link ledger, while
# an interface the panel never created is remembered by the address ledger and nowhere else.


def _failed_retarget_off_an_operator_link():
    """A move off the operator's own `eth0.2` whose new address the kernel refuses.

    `eth0.2` is on the host, up, and carrying the segment — and it is not in the link ledger, so
    nothing will ever delete it and no record of it survives the reconcile except the stale pair
    the refused replacement leaves. The pass itself covers both interfaces; the question these
    tests ask is what the renders AFTER it do.
    """
    host = LinkAndUninstallableAddrHost(
        links={"eth0", "eth0.2"}, up={"eth0", "eth0.2"},
        on={"eth0": {"192.168.1.20/24"}, "eth0.2": {"192.168.9.2/24"}},
        reject=["192.168.10.2/24"])
    store = _retarget_store(**{provision.LINK_KEY: ""})
    state = _State(store, LinuxBackend(host), _Dnsmasq())
    _seed_enforcement(state)

    assert not provision.host_provision(state).ok            # the address would not go on
    assert host.is_up("eth0.2") and "192.168.9.2/24" in host.addrs_on("eth0.2")
    assert store.get_setting("managed_segment_iface") == "eth0.9"     # ...and the record moved on
    assert provision._parse_stale(store) == [("eth0.2", "192.168.9.2/24")]
    return host, state


def test_a_failed_replace_keeps_the_old_interface_covered_by_the_next_render():
    # The rerender: any later `sync_net` — a kill-switch edit, a connect, a disconnect — renders the
    # enforcement from the store, and the store's segment interface is now the candidate.
    host, state = _failed_retarget_off_an_operator_link()

    result = sync_net(state)

    assert _enforced_ifaces(result.rendered) == {"eth0.9", "eth0.2"}
    assert _enforced_ifaces(_live_ruleset(state)) == {"eth0.9", "eth0.2"}
    _assert_segment_covered(host, state)


def test_a_failed_replace_keeps_the_old_interface_covered_across_a_reboot():
    # The reboot: nothing of the pass survives except the store, so a fresh backend with an empty
    # record of what it has loaded is the honest test — the ruleset this boot ends on has to name
    # the old interface because THIS boot rendered it that way, not because a previous pass did.
    host, state = _failed_retarget_off_an_operator_link()
    rebooted = _State(state.store, LinuxBackend(host), _Dnsmasq())

    provision.host_provision(rebooted)          # boot: the pass, then the leak-guard
    boot_guard(rebooted)

    assert host.is_up("eth0.2") and "192.168.9.2/24" in host.addrs_on("eth0.2")
    assert _enforced_ifaces(_live_ruleset(rebooted)) == {"eth0.9", "eth0.2"}
    _assert_segment_covered(host, rebooted)


def test_the_render_narrows_once_the_old_address_is_off_the_operator_link():
    # The property the durable cover may not cost: it drains. The next pass installs the addresses,
    # the removal it retries succeeds, nothing is left carrying the old segment — and the render
    # goes back to naming one interface.
    host, state = _failed_retarget_off_an_operator_link()
    host.reject.clear()

    assert provision.host_provision(state).ok
    result = sync_net(state)

    assert host.addrs_on("eth0.2") == set() and host.addrs_on("eth0.9") == {"192.168.10.2/24"}
    assert provision._parse_stale(state.store) == []
    assert provision.enforcement_cover(state.store, "eth0.9").names == []
    assert _enforced_ifaces(result.rendered) == {"eth0.9"}


def test_a_clean_move_leaves_nothing_in_the_cover_for_a_later_render_to_pick_up():
    # The whole-sequence version of the same guarantee: after a move that worked, every source of
    # the cover is empty, so the store renders one interface and keeps rendering one interface.
    _host, state, result, _killed = _run_move()

    assert result.ok
    assert provision.enforcement_cover(state.store, "eth0.9").names == []
    assert _enforced_ifaces(_live_ruleset(state)) == {"eth0.9"}
    assert _enforced_ifaces(sync_net(state).rendered) == {"eth0.9"}


def test_a_move_does_not_narrow_off_a_candidate_a_rolled_back_change_left_up(monkeypatch):
    # The pass's OWN narrowing consults the same cover. Here an earlier rolled-back change left the
    # operator's `eth1` up carrying the segment address, and then a perfectly ordinary move from
    # `eth0.2` to `eth0.9` completes: everything this pass touched is settled, so narrowing on "my
    # move is finished" would uncover `eth1`. The drop-in is the other way round — being named in a
    # rule costs an interface nothing, being listed unmanaged takes it away from NetworkManager — so
    # `eth1`, which the panel holds no address on, is never in it.
    written = []
    monkeypatch.setattr(provision, "_write_file", lambda path, text: written.append(text))
    host = LinkAndUninstallableAddrHost(
        links={"eth0", "eth0.2", "eth1"}, up={"eth0", "eth0.2", "eth1"},
        on={"eth0.2": {"192.168.9.2/24"}, "eth1": {"192.168.10.2/24"}})
    store = _retarget_store()
    store.set_setting(provision.SURVIVOR_KEY, "eth1 192.168.10.2/24")
    state = _State(store, LinuxBackend(host), _Dnsmasq())
    _seed_enforcement(state)

    result = provision.host_provision(state)

    assert result.ok
    assert "eth0.2" not in host.links                          # the move itself finished...
    assert host.is_up("eth0.9") and host.addrs_on("eth0.9") == {"192.168.10.2/24"}
    assert "eth1" in _enforced_ifaces(_live_ruleset(state))     # ...and did not uncover eth1
    assert _enforced_ifaces(sync_net(state).rendered) == {"eth0.9", "eth1"}
    _assert_segment_covered(host, state)
    assert not any("eth1" in text for text in written), \
        "an interface the panel holds no address on was taken away from NetworkManager"


# --- ...and the same narrowing, asked about a record the store will not READ -------------------
#
# Every source of the cover is a durable record, and a record has a third answer besides its content:
# the store may not be able to give it. That answer used to be spent as the content — the pending-undo
# reader caught every exception and returned `[]` — so "the panel cannot tell you which interfaces may
# still be carrying the segment" arrived at the narrowing decision as "no interface is", and the
# ruleset was narrowed onto the configured interface while another one was up and addressed. Which is
# the direct-WAN bypass, produced by a store read rather than by anything on the host.


def _read_fails_when(monkeypatch, store, key: str, when):
    """Make `get_setting` fail for `key` alone, from the moment `when()` says so.

    The narrowing decision lives in a window — the cover is read once to STAGE the transitional
    ruleset and again to decide whether it may be taken back — so a record that answers throughout
    cannot be asked what that decision does with "no answer". The host fact the test hangs it on is
    the commit point: the candidate coming up is the last thing before the narrow is considered.
    """
    original = store.get_setting

    def reading(name):
        if name == key and when():
            raise RuntimeError("simulated settings read failure")
        return original(name)

    monkeypatch.setattr(store, "get_setting", reading)


def test_a_move_does_not_narrow_when_the_pending_record_cannot_be_read(monkeypatch):
    # PRODUCT-CRITICAL, and the same host as the test above with the cover coming from the other
    # source. An earlier rolled-back change left the operator's `eth1` up carrying the segment
    # address; its survivor write never landed, so the SURVIVOR LEDGER IS EMPTY and the pending undo
    # record is the only thing that names the interface. An ordinary move from `eth0.2` to `eth0.9`
    # then completes, and the read of that record fails before the pass decides whether to narrow.
    #
    # The ruleset must stay as it was staged. Narrowing here leaves `eth1` up, addressed, and outside
    # the kill-switch drop and the tproxy redirect, for as long as the interface lasts.
    written = []
    monkeypatch.setattr(provision, "_write_file", lambda path, text: written.append(text))
    host = LinkAndUninstallableAddrHost(
        links={"eth0", "eth0.2", "eth1"}, up={"eth0", "eth0.2", "eth1"},
        on={"eth0.2": {"192.168.9.2/24"}, "eth1": {"192.168.10.2/24"}})
    store = _retarget_store()
    store.set_setting(provision.PROVISION_UNDO_KEY, json.dumps(
        {"iface": "eth1", "addr4": "192.168.10.2/24", "addr6": "", "vlan": False,
         "link_state": provision.LINK_PRESENT}))
    assert provision._parse_survivors(store) == [], "the ledger has to be empty for this to bite"
    state = _State(store, LinuxBackend(host), _Dnsmasq())
    _seed_enforcement(state)
    _read_fails_when(monkeypatch, store, provision.PROVISION_UNDO_KEY,
                     lambda: host.is_up("eth0.9"))

    result = provision.host_provision(state)

    assert host.is_up("eth0.9") and host.addrs_on("eth0.9") == {"192.168.10.2/24"}
    assert "eth1" in _enforced_ifaces(_live_ruleset(state)), \
        "a record the store would not read narrowed the ruleset off an interface that may be live"
    _assert_segment_covered(host, state)
    # ...and the pass says so, rather than reporting a success over a ruleset it cannot vouch for.
    assert result.ok is False
    assert "not narrowed" in result.error and "could not be read" in result.error


def test_a_move_will_not_start_when_the_cover_cannot_be_read(monkeypatch):
    # The other end of the same window. Here the record is unreadable from the start, so the
    # TRANSITIONAL ruleset — the thing that covers the candidate from the moment step 3 can make it
    # reachable — would be short. The move does not begin: nothing has been raised, the segment is
    # still on the interface the live ruleset names, and the pass reports instead of putting an
    # interface up that it cannot promise is covered.
    host = LinkAndUninstallableAddrHost(
        links={"eth0", "eth0.2"}, up={"eth0", "eth0.2"}, on={"eth0.2": {"192.168.9.2/24"}})
    store = _retarget_store()
    state = _State(store, LinuxBackend(host), _Dnsmasq())
    _seed_enforcement(state)
    _read_fails_when(monkeypatch, store, provision.PROVISION_UNDO_KEY, lambda: True)

    result = provision.host_provision(state)

    assert result.ok is False and "was not moved" in result.error
    assert host.addrs_on("eth0.2") == {"192.168.9.2/24"}      # the working segment is untouched
    assert not host.is_up("eth0.9")                            # ...and nothing was raised
    assert _enforced_ifaces(_live_ruleset(state)) == {"eth0.2"}


# --- ...and the same records read ONCE, transiently, before the pass decides anything -----------
#
# The two tests above interrupt a record for good. A store does not have to stay broken to cost the
# segment its cover, though, and the shorter failure is the more dangerous one: the pass asks what it
# is moving OFF exactly once, at the top, and every decision that follows — whether the transitional
# ruleset is staged at all, which interfaces the drop-in keeps from NetworkManager, whether the
# enforcement may be narrowed back — is taken from that one answer. Read as NAMES, an unread source
# arrives at all three as "the panel holds nothing anywhere else": the pass concludes it is not a
# move, stages nothing, and then addresses and RAISES the candidate under the ruleset the previous
# configuration left in force. Which names only the interface being left. The reads that follow
# succeed, so the record is perfectly readable by the time the addresses go on — the pass reports
# `ok=True`, and the bypass is live for as long as the interface is.


def _read_fails_once(monkeypatch, store, key: str) -> None:
    """Make `get_setting(key)` fail on its FIRST read and answer normally ever after.

    The transient store fault, in the one shape that matters here: gone by the time anything would
    notice it, having already been spent as an answer about the host.
    """
    original = store.get_setting
    spent: list[str] = []

    def reading(name):
        if name == key and not spent:
            spent.append(name)
            raise RuntimeError("simulated settings read failure")
        return original(name)

    monkeypatch.setattr(store, "get_setting", reading)


# The old interface is the operator's own in both cases — the panel created no link — so exactly one
# record names it, and a single failed read of that record is the whole difference between "there is
# nothing else out there" and "there is, and it is up".
_SOLE_RECORD = [
    ("the interface the last pass configured", "managed_segment_iface",
     {provision.LINK_KEY: "", "managed_segment_iface": "eth0.2"}),
    ("the stale address ledger", provision.STALE_KEY,
     {provision.LINK_KEY: "", "managed_segment_iface": "eth0.9",
      provision.STALE_KEY: "eth0.2 192.168.9.2/24"}),
]


@pytest.mark.parametrize("label, key, records", _SOLE_RECORD, ids=[r[0] for r in _SOLE_RECORD])
def test_a_transient_read_does_not_complete_a_move_with_the_candidate_uncovered(
        monkeypatch, label, key, records):
    # PRODUCT-CRITICAL. `eth0.2` is the operator's own interface, up and carrying the segment, and
    # `records` is the one record that says so. Its first read fails; every later read works.
    written = []
    monkeypatch.setattr(provision, "_write_file", lambda path, text: written.append(text))
    host = LinkAndUninstallableAddrHost(
        links={"eth0", "eth0.2"}, up={"eth0", "eth0.2"}, on={"eth0.2": {"192.168.9.2/24"}},
        refuse=[("eth0.2", "192.168.9.2/24")])       # the old address does not come off by itself
    store = _retarget_store(**records)
    state = _State(store, LinuxBackend(host), _Dnsmasq())
    _seed_enforcement(state)
    _read_fails_once(monkeypatch, store, key)

    result = provision.host_provision(state)

    # The move does not start: the candidate is not addressed, not raised, and not created.
    assert not any(cmd[:3] == ["ip", "addr", "replace"] for cmd in host.cmds())
    assert not host.is_up("eth0.9") and "eth0.9" not in host.links
    assert host.addrs_on("eth0.2") == {"192.168.9.2/24"}       # the working segment is untouched
    assert state.dnsmasq.applied == []
    # ...and the ruleset in force still covers the segment where it actually is.
    assert _enforced_ifaces(_live_ruleset(state)) == {"eth0.2"}
    _assert_segment_covered(host, state)
    # The NM drop-in is the other decision taken from that answer, and a short one hands the
    # interface the panel still addresses back to NetworkManager. It is not written at all.
    assert written == []
    assert result.ok is False
    assert "could not read which interfaces" in result.error
    assert "not applied" in result.error


def test_an_unknown_answer_that_still_names_an_interface_refuses_before_touching_the_host(
        monkeypatch):
    # The refusal is on `.known`, not on "the names came back empty" — a source that would not answer
    # may be the one naming a second live interface whatever the others said. Here the answer names
    # `eth0.2` AND is short, which is the state a names-only read cannot represent at all, and the
    # pass declines before it creates the candidate link: nothing on the host, and nothing in the
    # drop-in, may be decided from an answer the panel cannot vouch for.
    written = []
    monkeypatch.setattr(provision, "_write_file", lambda path, text: written.append(text))
    monkeypatch.setattr(provision, "superseded_state",
                        lambda store, iface: provision.Cover(["eth0.2"], ["simulated"]))
    host = LinkAndUninstallableAddrHost(
        links={"eth0", "eth0.2"}, up={"eth0", "eth0.2"}, on={"eth0.2": {"192.168.9.2/24"}})
    store = _retarget_store()
    state = _State(store, LinuxBackend(host), _Dnsmasq())
    _seed_enforcement(state)

    result = provision.host_provision(state)

    assert not any(cmd[:3] == ["ip", "link", "add"] for cmd in host.cmds())
    assert not any(cmd[:3] == ["ip", "addr", "replace"] for cmd in host.cmds())
    assert written == [] and state.dnsmasq.applied == []
    assert _enforced_ifaces(_live_ruleset(state)) == {"eth0.2"}
    assert result.ok is False and "simulated" in result.error
