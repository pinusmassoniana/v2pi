import ipaddress
import subprocess

import pytest

from pi_gw_panel.config import Settings, SETTINGS_DEFAULTS
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.net_control.plan import NetPlan
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.net_control.linux import TIMEOUT_RETURNCODE
from pi_gw_panel.net_control import pd_client, provision


def _store():
    conn = connect(":memory:")
    init_schema(conn)
    return NodeStore(conn)


class FakeRun:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, input=None):
        self.calls.append((cmd, input))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def cmds(self):
        return [c for c, _ in self.calls]


class FakeHost:
    """A runner whose `ip link add/delete` actually mutate a set of links, so a test can ask
    what is left on the host rather than only what was commanded. `ip link show` answers from
    that same set, so a probe for a link's absence gets the host's real answer."""

    def __init__(self, existing=()):
        self.links = set(existing)
        self.calls = []

    def __call__(self, cmd, input=None):
        self.calls.append(cmd)
        if cmd[:3] == ["ip", "link", "add"]:
            self.links.add(cmd[cmd.index("name") + 1])
        elif cmd[:3] == ["ip", "link", "delete"]:
            if cmd[3] not in self.links:
                raise subprocess.CalledProcessError(1, cmd)
            self.links.discard(cmd[3])
        elif cmd[:3] == ["ip", "link", "show"] and cmd[3] not in self.links:
            raise subprocess.CalledProcessError(
                1, cmd, output="", stderr=f'Device "{cmd[3]}" does not exist.')
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def exists(self, iface):
        return iface in self.links


class RefusingHost(FakeHost):
    """A host whose `ip link delete` fails for a reason that is NOT "already gone" — the link is
    still up afterwards. `returncode=TIMEOUT_RETURNCODE` is how the bounded runner reports a
    delete that never came back, which says nothing at all about the link."""

    def __init__(self, existing=(), refuse=(), returncode=1,
                 stderr="RTNETLINK answers: Operation not permitted"):
        super().__init__(existing)
        self.refuse, self.returncode, self.stderr = set(refuse), returncode, stderr

    def __call__(self, cmd, input=None):
        if cmd[:3] == ["ip", "link", "delete"] and cmd[3] in self.refuse:
            self.calls.append(cmd)
            raise subprocess.CalledProcessError(self.returncode, cmd, output="",
                                                stderr=self.stderr)
        return super().__call__(cmd, input)


class AddrHost:
    """A runner that models the ADDRESSES on an interface, the way `FakeHost` models the links.

    `ip addr replace/del` mutate the set, `ip -o addr show` answers from it, and a delete named in
    `refuse` fails while leaving the address exactly where it was — which is what EPERM and the
    runner's time limit both do, and the state the reconcile/clear paths have to survive.
    """

    def __init__(self, addrs=(), refuse=(), returncode=1,
                 stderr="RTNETLINK answers: Operation not permitted"):
        self.addrs = set(addrs)
        self.refuse = set(refuse)
        self.returncode, self.stderr = returncode, stderr
        self.calls = []

    def __call__(self, cmd, input=None):
        self.calls.append(cmd)
        rest = [tok for tok in cmd[1:] if tok not in ("-4", "-6", "-o")]
        if rest[:2] == ["addr", "del"]:
            if rest[2] in self.refuse:
                raise subprocess.CalledProcessError(self.returncode, cmd, output="",
                                                    stderr=self.stderr)
            self.addrs.discard(rest[2])
        elif rest[:2] == ["addr", "replace"]:
            self.addrs.add(rest[2])
        elif rest[:2] == ["addr", "show"]:
            ipv6 = "-6" in cmd
            return subprocess.CompletedProcess(cmd, 0, "".join(
                f"2: {rest[-1]}    {'inet6' if ':' in a else 'inet'} {a} scope global\n"
                for a in sorted(self.addrs) if (":" in a) == ipv6), "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def cmds(self):
        return list(self.calls)


class UnansweringHost(FakeHost):
    """A host that answers NEITHER half: `ip link delete` and `ip link show` both hit the runner's
    time limit, which is how a wedged netlink presents. `TIMEOUT_RETURNCODE` + the runner's own
    synthetic stderr, so this is the exact exception `provision`'s default seam sees in
    production — no `link_exists` is injected against this fake, deliberately."""

    def __init__(self, existing=(), unanswered=()):
        super().__init__(existing)
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


class LinuxBackend:                 # name + `_run` seam = the provision linux gate
    def __init__(self, run):
        self._run = run


class _Dnsmasq:
    def __init__(self):
        self.applied = []
        self.stopped = 0

    def apply(self, text):
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
    def __init__(self, store, net, dnsmasq=None):
        self.store, self.net, self.dnsmasq = store, net, dnsmasq
        self.settings = Settings()


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
    assert ["ip", "link", "set", "eth0.2", "up"] in cmds
    assert store.get_setting("managed_segment_link") == "eth0.2"


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
    kernel would not take, which is a warning and not a failure to apply.)
    """

    def __init__(self, addrs=(), reject=(), **kwargs):
        super().__init__(addrs=addrs, **kwargs)
        self.reject = set(reject)

    def __call__(self, cmd, input=None):
        rest = [tok for tok in cmd[1:] if tok not in ("-4", "-6", "-o")]
        if rest[:2] == ["addr", "replace"] and rest[2] in self.reject:
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
        rest = [tok for tok in cmd[1:] if tok not in ("-4", "-6", "-o")]
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
        if cmd[:3] == ["ip", "link", "add"]:
            self.calls.append(cmd)
            self.links.add(cmd[cmd.index("name") + 1])
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:3] == ["ip", "link", "delete"]:
            self.calls.append(cmd)
            self.links.discard(cmd[3])
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:3] == ["ip", "link", "show"] and cmd[3] not in self.links:
            self.calls.append(cmd)
            raise subprocess.CalledProcessError(
                1, cmd, output="", stderr=f'Device "{cmd[3]}" does not exist.')
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
    assert host.addrs == owed | {"192.168.10.2/24"}    # the superseded address went; nothing else
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
    host.addrs.add("192.168.99.2/24")
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
    host.addrs |= owed
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
        rest = [tok for tok in cmd[1:] if tok not in ("-4", "-6", "-o")]
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
        rest = [tok for tok in cmd[1:] if tok not in ("-4", "-6", "-o")]
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
