import json
import subprocess
import pytest
from pi_gw_panel.config import Settings
from pi_gw_panel.net_control.plan import NetPlan
from pi_gw_panel.net_control.linux import LinuxBackend
from pi_gw_panel.net_control.factory import select_backend

_RULE_JSON = json.dumps([{"priority": 100, "src": "all", "fwmark": "0x40", "table": "100"}])
_ROUTE_JSON = json.dumps([{"type": "local", "dst": "default", "dev": "lo", "scope": "host"}])


class FakeRun:
    """Records (cmd, input) per call; raises CalledProcessError when `fail` is in cmd
    (simulates a missing rule / bad ruleset). The injectable seam for LinuxBackend."""

    def __init__(self, fail=None, stderr="boom", rules=_RULE_JSON, routes=_ROUTE_JSON):
        self.calls: list[tuple[list[str], str | None]] = []
        self.fail = fail
        self.stderr = stderr
        self.rules = rules
        self.routes = routes

    def __call__(self, cmd, input=None):
        self.calls.append((cmd, input))
        if self.fail is not None and self.fail in cmd:
            raise subprocess.CalledProcessError(1, cmd, stderr=self.stderr)
        stdout = ""
        if cmd[-2:] == ["rule", "show"]:
            stdout = self.rules
        elif cmd[-4:] == ["route", "show", "table", "100"]:
            stdout = self.routes
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    def cmds(self) -> list[list[str]]:
        return [c for c, _ in self.calls]


def _backend(fake):
    return LinuxBackend(Settings(), run=fake, write_proc=lambda path, value: True)


def test_apply_loads_nft_ruleset_and_policy_routing():
    fake = FakeRun()
    res = _backend(fake).apply_tproxy(NetPlan.from_settings(Settings()))
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
    _backend(fake).apply_tproxy(NetPlan.from_settings(Settings()))
    cmds = fake.cmds()
    # a best-effort `del` precedes the `add` so re-apply never stacks duplicate fwmark rules
    assert ["ip", "rule", "del", "fwmark", "0x40", "lookup", "100"] in cmds
    assert cmds.index(["ip", "rule", "del", "fwmark", "0x40", "lookup", "100"]) < \
           cmds.index(["ip", "rule", "add", "fwmark", "0x40", "lookup", "100"])


def test_apply_returns_error_when_nft_fails():
    fake = FakeRun(fail="nft", stderr="nft: syntax error")
    res = _backend(fake).apply_tproxy(NetPlan.from_settings(Settings()))
    assert res.ok is False
    assert "syntax error" in res.error


def test_teardown_removes_table_rule_and_route_best_effort():
    # even if every `ip` call errors because the rule/route is already absent, teardown still
    # reports ok (an "already absent" nft/ip error is not a real failure — stderr says "no such")
    fake = FakeRun(fail="ip", stderr="Error: No such file or directory")
    res = _backend(fake).teardown()
    assert res.ok is True
    cmds = fake.cmds()
    assert ["nft", "delete", "table", "ip", "pi_gw_panel"] in cmds
    assert any(c[:3] == ["ip", "rule", "del"] for c in cmds)
    assert ["ip", "route", "del", "local", "default", "dev", "lo", "table", "100"] in cmds


def test_cleanup_deletes_only_our_route_never_flushes_the_policy_table():
    # The panel installs exactly one route in table 100. Flushing the table would also destroy
    # any route an operator or another daemon keeps there.
    fake = FakeRun()
    _backend(fake).teardown()
    assert not any("flush" in c for c in fake.cmds())


def test_teardown_reports_policy_cleanup_permission_failure():
    fake = FakeRun(fail="ip", stderr="RTNETLINK answers: Operation not permitted")
    res = _backend(fake).teardown()
    assert res.ok is False
    assert "Operation not permitted" in res.error


def test_failed_partial_apply_does_not_delete_enforcement_tables():
    fake = FakeRun(fail="ip", stderr="RTNETLINK answers: Operation not permitted")
    plan = NetPlan.from_settings(Settings())
    plan.kill_switch = True
    res = _backend(fake).apply_tproxy(plan)
    assert res.ok is False
    assert not any(c[:4] == ["nft", "delete", "table", "ip"] for c in fake.cmds())


def test_factory_linux_returns_linuxbackend(monkeypatch):
    monkeypatch.setenv("PI_GW_NET_BACKEND", "linux")
    assert type(select_backend(Settings())).__name__ == "LinuxBackend"


# --- LAN access: segment → home-LAN rules in the panel-owned chain ---
_FWD = ["PI_GW_PANEL", "-i", "eth0.2", "-o", "eth0", "-d", "192.168.1.0/24", "-j", "ACCEPT"]
_RET = ["PI_GW_PANEL", "-i", "eth0", "-o", "eth0.2",
        "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"]


def test_apply_lan_access_on_inserts_docker_user_forward_rules():
    fake = FakeRun()
    _backend(fake).apply_tproxy(NetPlan.from_settings(Settings()))  # lan_access on
    cmds = fake.cmds()
    assert ["iptables", "-A", *_FWD] in cmds          # segment → LAN
    assert ["iptables", "-A", *_RET] in cmds          # established return
    assert ["iptables", "-F", "PI_GW_PANEL"] in cmds  # stale interface tuples are removed
    assert ["iptables", "-I", "DOCKER-USER", "1", "-j", "PI_GW_PANEL"] in cmds
    # scoped to the LAN cidr — the rule never names a WAN/0.0.0.0 dest
    assert all("0.0.0.0/0" not in c for c in cmds if c[:2] == ["iptables", "-A"])


def test_apply_lan_access_off_removes_but_never_inserts():
    fake = FakeRun()
    plan = NetPlan.from_settings(Settings())
    plan.lan_access = False
    _backend(fake).apply_tproxy(plan)
    cmds = fake.cmds()
    assert not any(c[:3] == ["iptables", "-A", "PI_GW_PANEL"] for c in cmds)
    assert ["iptables", "-F", "PI_GW_PANEL"] in cmds                           # stale copy cleared


def test_teardown_removes_lan_access_forward_rules():
    fake = FakeRun()
    _backend(fake).teardown()
    cmds = fake.cmds()
    assert ["iptables", "-D", "DOCKER-USER", "-j", "PI_GW_PANEL"] in cmds
    assert ["iptables", "-F", "PI_GW_PANEL"] in cmds
    assert ["iptables", "-X", "PI_GW_PANEL"] in cmds


# --- a failed apply must not leave the segment tproxy'd into a black hole ---

class _SelectiveRun(FakeRun):
    """FakeRun that fails only the exact argv prefixes given."""

    def __init__(self, *prefixes, stderr="boom", **kw):
        super().__init__(stderr=stderr, **kw)
        self.prefixes = prefixes

    def __call__(self, cmd, input=None):
        for prefix in self.prefixes:
            if cmd[:len(prefix)] == list(prefix):
                self.calls.append((cmd, input))
                raise subprocess.CalledProcessError(1, cmd, stderr=self.stderr)
        return super().__call__(cmd, input)


def test_failed_tproxy_apply_reinstates_the_fail_closed_guard():
    # nft loads the tproxy ruleset FIRST; a failure in the policy routing that follows would
    # otherwise leave clients tproxy'd with no ip rule / local route at all — black-holed.
    fake = _SelectiveRun(["ip", "rule", "add"], stderr="RTNETLINK answers: not permitted")
    plan = NetPlan.from_settings(Settings())
    plan.kill_switch = True
    res = _backend(fake).apply_tproxy(plan)
    assert res.ok is False
    scripts = [i for c, i in fake.calls if c[:2] == ["nft", "-f"]]
    assert len(scripts) == 2, "expected the guard ruleset to be reloaded after the failure"
    assert "tproxy ip to" in scripts[0]
    assert "tproxy ip to" not in scripts[1]      # no tproxy left pointing at a missing route
    assert "chain forward" in scripts[1]         # kill-switch drop still enforced


def test_guard_fallback_failure_is_reported_alongside_the_original_error():
    fake = _SelectiveRun(["ip"], stderr="RTNETLINK answers: not permitted")
    res = _backend(fake).apply_tproxy(NetPlan.from_settings(Settings()))
    assert res.ok is False
    assert "fail-closed fallback also failed" in res.error


# --- LAN access is secondary: a warning, and no DOCKER-USER dependency when off ---

def test_lan_access_off_does_not_require_a_docker_user_chain():
    # A non-Docker host has no DOCKER-USER chain. With LAN access off the panel must not need
    # it at all, or the tunnel can never be brought up there.
    fake = _SelectiveRun(["iptables", "-I", "DOCKER-USER"],
                         stderr="iptables: No chain/target/match by that name.")
    plan = NetPlan.from_settings(Settings())
    plan.lan_access = False
    res = _backend(fake).apply_tproxy(plan)
    assert res.ok is True and res.warning == ""


def test_lan_access_insert_failure_is_a_warning_not_a_failed_apply():
    fake = _SelectiveRun(["iptables", "-I", "DOCKER-USER"],
                         stderr="iptables: No chain/target/match by that name.")
    res = _backend(fake).apply_tproxy(NetPlan.from_settings(Settings()))   # lan_access on
    assert res.ok is True
    assert "LAN access chain not applied" in res.warning


# --- post-apply verification must match structurally, not by substring ---

def test_verify_rejects_rules_that_only_mention_the_table_number():
    # A text search passes on this: "0x40" and "100" both appear, just never on one rule.
    fake = FakeRun(rules=json.dumps([
        {"priority": 100, "src": "all", "table": "main"},
        {"priority": 32765, "src": "all", "fwmark": "0x40", "table": "50"},
    ]))
    res = _backend(fake).apply_tproxy(NetPlan.from_settings(Settings()))
    assert res.ok is False
    assert "fwmark" in res.error


def test_verify_rejects_a_local_route_for_some_other_address():
    # Likewise `"lo" in routes` is satisfied by the word "local" alone.
    fake = FakeRun(routes=json.dumps([{"type": "local", "dst": "10.1.2.3", "dev": "eth0"}]))
    res = _backend(fake).apply_tproxy(NetPlan.from_settings(Settings()))
    assert res.ok is False
    assert "local route" in res.error


def test_verify_accepts_the_real_fwmark_rule_and_local_route():
    fake = FakeRun()
    assert _backend(fake).apply_tproxy(NetPlan.from_settings(Settings())).ok is True
    assert ["ip", "-j", "rule", "show"] in fake.cmds()
