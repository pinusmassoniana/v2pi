import ipaddress
import subprocess

from pi_gw_panel.config import Settings, SETTINGS_DEFAULTS
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.net_control.plan import NetPlan
from pi_gw_panel.net_control.dryrun import DryRunBackend
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
