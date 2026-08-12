import ipaddress
import subprocess

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
        provision.ensure_segment_link(store, _plan_for(iface), run=host,
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

    assert host.links == {"eth0", "eth0.9"}
    assert provision._parse_links(store) == ["eth0.9"]


def test_a_link_the_panel_did_not_create_is_never_deleted():
    host = FakeHost(existing=["eth0", "eth0.2"])             # eth0.2 pre-dates the panel
    store = _store()

    provision.ensure_segment_link(store, _plan_for("eth0.2"), run=host,
                                  link_exists=host.exists)
    provision.ensure_segment_link(store, _plan_for("eth0.9"), run=host,
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

    failed = provision.ensure_segment_link(store, _plan_for("eth0.9"), run=host,
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
