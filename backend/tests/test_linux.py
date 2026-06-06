import subprocess
import pytest
from pi_gw_panel.config import Settings
from pi_gw_panel.net_control.plan import NetPlan
from pi_gw_panel.net_control.linux import LinuxBackend
from pi_gw_panel.net_control.factory import select_backend


class FakeRun:
    """Records (cmd, input) per call; raises CalledProcessError when `fail` is in cmd
    (simulates a missing rule / bad ruleset). The injectable seam for LinuxBackend."""

    def __init__(self, fail=None, stderr="boom"):
        self.calls: list[tuple[list[str], str | None]] = []
        self.fail = fail
        self.stderr = stderr

    def __call__(self, cmd, input=None):
        self.calls.append((cmd, input))
        if self.fail is not None and self.fail in cmd:
            raise subprocess.CalledProcessError(1, cmd, stderr=self.stderr)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def cmds(self) -> list[list[str]]:
        return [c for c, _ in self.calls]


def test_apply_loads_nft_ruleset_and_policy_routing():
    fake = FakeRun()
    res = LinuxBackend(Settings(), run=fake).apply_tproxy(NetPlan.from_settings(Settings()))
    assert res.ok is True
    # nft -f loaded the rendered tproxy table over stdin
    nft = [(c, i) for c, i in fake.calls if c[:2] == ["nft", "-f"]]
    assert nft, "expected an `nft -f` load"
    script = nft[0][1]
    assert "table ip pi_gw_panel" in script
    assert "tproxy ip to :52345" in script
    # policy routing: fwmark rule + table-100 local default so marked packets are delivered locally
    cmds = fake.cmds()
    assert ["ip", "rule", "add", "fwmark", "0x40", "lookup", "100"] in cmds
    assert ["ip", "route", "replace", "local", "default", "dev", "lo", "table", "100"] in cmds


def test_apply_dedupes_ip_rule_before_adding():
    fake = FakeRun()
    LinuxBackend(Settings(), run=fake).apply_tproxy(NetPlan.from_settings(Settings()))
    cmds = fake.cmds()
    # a best-effort `del` precedes the `add` so re-apply never stacks duplicate fwmark rules
    assert ["ip", "rule", "del", "fwmark", "0x40", "lookup", "100"] in cmds
    assert cmds.index(["ip", "rule", "del", "fwmark", "0x40", "lookup", "100"]) < \
           cmds.index(["ip", "rule", "add", "fwmark", "0x40", "lookup", "100"])


def test_apply_returns_error_when_nft_fails():
    fake = FakeRun(fail="nft", stderr="nft: syntax error")
    res = LinuxBackend(Settings(), run=fake).apply_tproxy(NetPlan.from_settings(Settings()))
    assert res.ok is False
    assert "syntax error" in res.error


def test_teardown_removes_table_rule_and_route_best_effort():
    # even if every `ip` call errors (rule/route already absent), teardown still reports ok
    fake = FakeRun(fail="ip")
    res = LinuxBackend(Settings(), run=fake).teardown()
    assert res.ok is True
    cmds = fake.cmds()
    assert ["nft", "delete", "table", "ip", "pi_gw_panel"] in cmds
    assert any(c[:3] == ["ip", "rule", "del"] for c in cmds)
    assert any(c[:3] == ["ip", "route", "flush"] for c in cmds)


def test_factory_linux_returns_linuxbackend(monkeypatch):
    monkeypatch.setenv("PI_GW_NET_BACKEND", "linux")
    assert type(select_backend(Settings())).__name__ == "LinuxBackend"


# --- LAN access: segment → home-LAN forward accepts in DOCKER-USER ---
_FWD = ["DOCKER-USER", "-i", "eth0.2", "-o", "eth0", "-d", "192.168.1.0/24", "-j", "ACCEPT"]
_RET = ["DOCKER-USER", "-i", "eth0", "-o", "eth0.2",
        "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"]


def test_apply_lan_access_on_inserts_docker_user_forward_rules():
    fake = FakeRun()
    LinuxBackend(Settings(), run=fake).apply_tproxy(NetPlan.from_settings(Settings()))  # lan_access on
    cmds = fake.cmds()
    assert ["iptables", "-I", *_FWD] in cmds          # segment → LAN
    assert ["iptables", "-I", *_RET] in cmds          # established return
    # idempotent: a `-D` precedes each `-I` so re-apply never stacks duplicates
    assert ["iptables", "-D", *_FWD] in cmds
    assert cmds.index(["iptables", "-D", *_FWD]) < cmds.index(["iptables", "-I", *_FWD])
    # scoped to the LAN cidr — the rule never names a WAN/0.0.0.0 dest
    assert all("0.0.0.0/0" not in c for c in cmds if c[:2] == ["iptables", "-I"])


def test_apply_lan_access_off_removes_but_never_inserts():
    fake = FakeRun()
    plan = NetPlan.from_settings(Settings())
    plan.lan_access = False
    LinuxBackend(Settings(), run=fake).apply_tproxy(plan)
    cmds = fake.cmds()
    assert not any(c[:3] == ["iptables", "-I", "DOCKER-USER"] for c in cmds)   # never opened
    assert ["iptables", "-D", *_FWD] in cmds                                   # stale copy cleared


def test_teardown_removes_lan_access_forward_rules():
    fake = FakeRun()
    LinuxBackend(Settings(), run=fake).teardown()
    cmds = fake.cmds()
    assert ["iptables", "-D", *_FWD] in cmds
    assert ["iptables", "-D", *_RET] in cmds
