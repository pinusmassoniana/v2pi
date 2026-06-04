import subprocess
from pi_gw_panel.net_control.plan import NetPlan, NetResult
from pi_gw_panel.net_control.render import render_nft, render_nft6

NFT_TABLE = "pi_gw_panel"


def _run(cmd: list[str], input: str | None = None) -> subprocess.CompletedProcess:
    """Default runner: subprocess.run with check; raises CalledProcessError on non-zero."""
    return subprocess.run(cmd, input=input, capture_output=True, text=True, check=True)


def _write_proc(path: str, value: str) -> None:
    """Best-effort write a sysctl /proc file (absent on dev → ignored)."""
    try:
        with open(path, "w") as f:
            f.write(value)
    except OSError:
        pass


class LinuxBackend:
    """Real Pi backend: applies the rendered nft tproxy ruleset + tproxy policy routing
    to the host netns (the panel runs host-net + NET_ADMIN, so these affect the host).

    DHCP/DNS is intentionally NOT managed here — the host's ``pi-gw-dhcp.service`` owns
    the segment's DHCP. ``run`` is the injectable subprocess seam for tests.
    """

    def __init__(self, settings, run=_run, write_proc=None):
        self.settings = settings
        self._run = run
        self._write_proc = write_proc or _write_proc

    def _nft_script(self, plan: NetPlan, tunnel_up: bool) -> str:
        """Atomic idempotent (re)load of both families. `add table` makes the following
        `delete table` safe on first run; the render then recreates each table fresh (the
        v6 table is recreated only when the kill-switch is on, else left deleted)."""
        v4 = render_nft(plan, tunnel_up=tunnel_up)
        v6 = render_nft6(plan, tunnel_up=tunnel_up)
        script = f"add table ip {NFT_TABLE}\ndelete table ip {NFT_TABLE}\n{v4}"
        script += f"add table ip6 {NFT_TABLE}\ndelete table ip6 {NFT_TABLE}\n"
        if v6:
            script += v6
        return script

    def apply_tproxy(self, plan: NetPlan) -> NetResult:
        nft_text = render_nft(plan)
        fw, tbl = f"0x{plan.fwmark:x}", str(plan.table)
        try:
            self._run(["nft", "-f", "-"], input=self._nft_script(plan, tunnel_up=True))
            # Policy routing: deliver fwmark'd packets locally (table 100 → lo) so xray's
            # tproxy socket receives them. del-before-add keeps it to exactly one rule.
            self._run_ok(["ip", "rule", "del", "fwmark", fw, "lookup", tbl])
            self._run(["ip", "rule", "add", "fwmark", fw, "lookup", tbl])
            self._run(["ip", "route", "replace", "local", "default", "dev", "lo", "table", tbl])
            if plan.ipv6_enabled:           # mirror the policy routing for v6 tproxy
                self._run_ok(["ip", "-6", "rule", "del", "fwmark", fw, "lookup", tbl])
                self._run(["ip", "-6", "rule", "add", "fwmark", fw, "lookup", tbl])
                self._run(["ip", "-6", "route", "replace", "local", "default", "dev", "lo", "table", tbl])
            else:                            # E: drop any stale v6 routing left from a prior v6 on
                self._remove_v6_policy_routing(plan.fwmark, plan.table)
            self._ensure_forward(ipv6=plan.ipv6_enabled)
            return NetResult(ok=True, rendered=nft_text)
        except subprocess.CalledProcessError as exc:
            return NetResult(ok=False, rendered=nft_text,
                             error=(exc.stderr or str(exc)).strip())

    def apply_guard(self, plan: NetPlan) -> NetResult:
        """Fail-closed leak-guard (A1): install the kill-switch drop (v4 + v6) with NO
        tproxy/policy-routing — for when the tunnel is intentionally stopped but the
        kill-switch must keep blocking client→WAN. With the kill-switch off this is an
        empty table (effectively a teardown of the tproxy rules)."""
        nft_text = render_nft(plan, tunnel_up=False)
        try:
            self._run(["nft", "-f", "-"], input=self._nft_script(plan, tunnel_up=False))
            self._remove_policy_routing(plan.fwmark, plan.table)   # no tproxy → drop the fwmark route
            return NetResult(ok=True, rendered=nft_text)
        except subprocess.CalledProcessError as exc:
            return NetResult(ok=False, rendered=nft_text,
                             error=(exc.stderr or str(exc)).strip())

    def teardown(self) -> NetResult:
        """Best-effort remove (ignore-if-absent) — the rollback + no-kill-switch stop path."""
        self._run_ok(["nft", "delete", "table", "ip", NFT_TABLE])
        self._run_ok(["nft", "delete", "table", "ip6", NFT_TABLE])
        self._remove_policy_routing(self.settings.fwmark, self.settings.table)
        return NetResult(ok=True)

    def _remove_policy_routing(self, fwmark: int, table: int) -> None:
        fw, tbl = f"0x{fwmark:x}", str(table)
        self._run_ok(["ip", "rule", "del", "fwmark", fw, "lookup", tbl])
        self._run_ok(["ip", "route", "flush", "table", tbl])
        self._remove_v6_policy_routing(fwmark, table)

    def _remove_v6_policy_routing(self, fwmark: int, table: int) -> None:
        fw, tbl = f"0x{fwmark:x}", str(table)
        self._run_ok(["ip", "-6", "rule", "del", "fwmark", fw, "lookup", tbl])   # no-op if absent
        self._run_ok(["ip", "-6", "route", "flush", "table", tbl])

    def _run_ok(self, cmd: list[str]) -> None:
        """Run ignoring a non-zero exit (the rule/table is already absent)."""
        try:
            self._run(cmd)
        except subprocess.CalledProcessError:
            pass

    def _ensure_forward(self, ipv6: bool = False) -> None:
        """Ensure IPv4 (and, when tunnelling v6, IPv6) forwarding (best-effort)."""
        self._write_proc("/proc/sys/net/ipv4/ip_forward", "1")
        if ipv6:
            self._write_proc("/proc/sys/net/ipv6/conf/all/forwarding", "1")
            # D: enabling v6 forwarding makes the kernel stop accepting RAs by default, so the
            # Pi's Home leg can lose its own v6 (address + default route → no v6 egress).
            # accept_ra=2 keeps accepting RA on the uplink even while forwarding.
            self._write_proc(f"/proc/sys/net/ipv6/conf/{self.settings.mgmt_iface}/accept_ra", "2")
