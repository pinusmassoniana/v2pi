import subprocess
from pi_gw_panel.net_control.plan import NetPlan, NetResult
from pi_gw_panel.net_control.render import render_nft

NFT_TABLE = "pi_gw_panel"


def _run(cmd: list[str], input: str | None = None) -> subprocess.CompletedProcess:
    """Default runner: subprocess.run with check; raises CalledProcessError on non-zero."""
    return subprocess.run(cmd, input=input, capture_output=True, text=True, check=True)


class LinuxBackend:
    """Real Pi backend: applies the rendered nft tproxy ruleset + tproxy policy routing
    to the host netns (the panel runs host-net + NET_ADMIN, so these affect the host).

    DHCP/DNS is intentionally NOT managed here — the host's ``pi-gw-dhcp.service`` owns
    the segment's DHCP. ``run`` is the injectable subprocess seam for tests.
    """

    def __init__(self, settings, run=_run):
        self.settings = settings
        self._run = run

    def apply_tproxy(self, plan: NetPlan) -> NetResult:
        nft_text = render_nft(plan)
        fw, tbl = f"0x{plan.fwmark:x}", str(plan.table)
        try:
            # Atomic idempotent (re)load: `add table` makes the following `delete table`
            # safe on first run, then the render recreates the table fresh.
            script = f"add table ip {NFT_TABLE}\ndelete table ip {NFT_TABLE}\n{nft_text}"
            self._run(["nft", "-f", "-"], input=script)
            # Policy routing: deliver fwmark'd packets locally (table 100 → lo) so xray's
            # tproxy socket receives them. del-before-add keeps it to exactly one rule.
            self._run_ok(["ip", "rule", "del", "fwmark", fw, "lookup", tbl])
            self._run(["ip", "rule", "add", "fwmark", fw, "lookup", tbl])
            self._run(["ip", "route", "replace", "local", "default", "dev", "lo", "table", tbl])
            self._ensure_forward()
            return NetResult(ok=True, rendered=nft_text)
        except subprocess.CalledProcessError as exc:
            return NetResult(ok=False, rendered=nft_text,
                             error=(exc.stderr or str(exc)).strip())

    def teardown(self) -> NetResult:
        """Best-effort remove (ignore-if-absent) — the rollback + kill-switch path."""
        fw, tbl = f"0x{self.settings.fwmark:x}", str(self.settings.table)
        self._run_ok(["nft", "delete", "table", "ip", NFT_TABLE])
        self._run_ok(["ip", "rule", "del", "fwmark", fw, "lookup", tbl])
        self._run_ok(["ip", "route", "flush", "table", tbl])
        return NetResult(ok=True)

    def _run_ok(self, cmd: list[str]) -> None:
        """Run ignoring a non-zero exit (the rule/table is already absent)."""
        try:
            self._run(cmd)
        except subprocess.CalledProcessError:
            pass

    def _ensure_forward(self) -> None:
        """Ensure IPv4 forwarding (best-effort; usually already on)."""
        try:
            with open("/proc/sys/net/ipv4/ip_forward", "w") as f:
                f.write("1")
        except OSError:
            pass
