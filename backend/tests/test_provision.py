import ipaddress
import subprocess

from pi_gw_panel.config import Settings, SETTINGS_DEFAULTS
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.net_control.plan import NetPlan
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.net_control import provision


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


class LinuxBackend:                 # name + `_run` seam = the provision linux gate
    def __init__(self, run):
        self._run = run


class _Dnsmasq:
    def __init__(self):
        self.applied = []

    def apply(self, text):
        self.applied.append(text)


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


def test_ensure_segment_iface_creates_vlan_and_addresses():
    fake = FakeRun()
    p = NetPlan.from_settings(Settings())
    provision.ensure_segment_iface(p, run=fake, link_exists=lambda i: False)
    cmds = fake.cmds()
    assert ["ip", "link", "add", "link", "eth0", "name", "eth0.2", "type", "vlan", "id", "2"] in cmds
    assert ["ip", "addr", "replace", "192.168.10.2/24", "dev", "eth0.2"] in cmds
    assert ["ip", "link", "set", "eth0.2", "up"] in cmds


def test_ensure_segment_iface_skips_link_add_when_present_and_adds_v6():
    fake = FakeRun()
    p = NetPlan.from_settings(Settings())
    p.ipv6_enabled = True
    p.segment_ip6 = "fd00:1:2:3::/64"
    provision.ensure_segment_iface(p, run=fake, link_exists=lambda i: True)
    cmds = fake.cmds()
    assert not any(c[:3] == ["ip", "link", "add"] for c in cmds)
    assert ["ip", "-6", "addr", "replace", "fd00:1:2:3::1/64", "dev", "eth0.2"] in cmds


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


def test_ensure_segment_prefix6_noop_when_v6_off():
    store = _store()
    assert provision.ensure_segment_prefix6(store, Settings()) == ""
    assert store.get_setting("segment_ip6") in (None, "")


def test_host_provision_runs_full_chain_on_linux():
    fake = FakeRun()
    store = _store()
    store.set_setting("manage_segment", "1")
    dnsmasq = _Dnsmasq()
    provision.host_provision(_State(store, LinuxBackend(fake), dnsmasq))
    cmds = fake.cmds()
    assert any(c[:2] == ["ip", "addr"] for c in cmds)
    assert dnsmasq.applied and "interface=eth0.2" in dnsmasq.applied[-1]


def test_host_provision_noop_on_dryrun_backend():
    store = _store()
    provision.host_provision(_State(store, DryRunBackend(), _Dnsmasq()))   # no `_run` -> skip


def test_host_provision_skipped_when_manage_segment_off():
    fake = FakeRun()
    store = _store()
    store.set_setting("manage_segment", "0")
    dnsmasq = _Dnsmasq()
    provision.host_provision(_State(store, LinuxBackend(fake), dnsmasq))
    assert fake.cmds() == [] and dnsmasq.applied == []


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
    started = []
    state = _State(store, LinuxBackend(fake), _Dnsmasq())
    state.pd_client = type("PD", (), {"start": lambda self: started.append(1)})()
    provision.host_provision(state)
    assert started == [1]
