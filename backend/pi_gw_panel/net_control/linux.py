import subprocess
from pi_gw_panel.net_control.plan import NetPlan, NetResult, net24
from pi_gw_panel.net_control.render import render_nft, render_nft6

NFT_TABLE = "pi_gw_panel"


def _lan_forward_rules(seg_if: str, lan_if: str, lan_cidr: str) -> list[list[str]]:
    """The two DOCKER-USER forward bodies that let segment↔home-LAN traffic past Docker's
    `filter FORWARD policy=drop` (the panel's own nft forward chain can't override another base
    chain's drop). Shared by the apply (`-I`) and the remove (`-D`) so they always match."""
    return [
        ["DOCKER-USER", "-i", seg_if, "-o", lan_if, "-d", lan_cidr, "-j", "ACCEPT"],
        ["DOCKER-USER", "-i", lan_if, "-o", seg_if,
         "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
    ]


def _run(cmd: list[str], input: str | None = None) -> subprocess.CompletedProcess:
    """Default runner: subprocess.run with check; raises CalledProcessError on non-zero."""
    return subprocess.run(cmd, input=input, capture_output=True, text=True, check=True)


def _write_proc(path: str, value: str) -> bool:
    """Write a sysctl /proc file; return True on success. Absent on dev → False (caller decides)."""
    try:
        with open(path, "w") as f:
            f.write(value)
        return True
    except OSError:
        return False


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
        # report the full ruleset actually loaded (v4 + v6), not just the v4 table, so audit
        # logs / UI match what's live (matters when debugging a v6 leak).
        nft_text = self._nft_script(plan, tunnel_up=True)
        fw, tbl = f"0x{plan.fwmark:x}", str(plan.table)
        try:
            self._run(["nft", "-f", "-"], input=nft_text)
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
            warn = "; ".join(w for w in (
                self._ensure_forward(ipv6=plan.ipv6_enabled),
                self._apply_lan_access(plan),
            ) if w)
            return NetResult(ok=True, rendered=nft_text, warning=warn)
        except (subprocess.CalledProcessError, OSError) as exc:
            # partial apply leaves marked-but-unrouted packets → segment blackholes; roll the host
            # back to a clean state before reporting failure so we never leave a half-applied mess.
            self._safe_teardown()
            return NetResult(ok=False, rendered=nft_text,
                             error=(getattr(exc, "stderr", None) or str(exc)).strip())

    def apply_guard(self, plan: NetPlan) -> NetResult:
        """Fail-closed leak-guard (A1): install the kill-switch drop (v4 + v6) with NO
        tproxy/policy-routing — for when the tunnel is intentionally stopped but the
        kill-switch must keep blocking client→WAN. With the kill-switch off this is an
        empty table (effectively a teardown of the tproxy rules)."""
        nft_text = self._nft_script(plan, tunnel_up=False)
        try:
            self._run(["nft", "-f", "-"], input=nft_text)
            self._remove_policy_routing(plan.fwmark, plan.table)   # no tproxy → drop the fwmark route
            self._apply_lan_access(plan)            # LAN access is independent of tunnel state
            return NetResult(ok=True, rendered=nft_text)
        except (subprocess.CalledProcessError, OSError) as exc:
            return NetResult(ok=False, rendered=nft_text,
                             error=(getattr(exc, "stderr", None) or str(exc)).strip())

    def _safe_teardown(self) -> None:
        """Best-effort teardown used on the apply-failure path; never raises."""
        try:
            self.teardown()
        except Exception:
            pass

    def teardown(self) -> NetResult:
        """Best-effort remove (ignore-if-absent) — the rollback + no-kill-switch stop path.
        Distinguishes 'already absent' (fine) from a command that actually failed, so the caller
        isn't told the rollback succeeded while stale rules (e.g. a kill-switch drop) remain."""
        failed: list[str] = []
        self._del_table(failed, "ip")
        self._del_table(failed, "ip6")
        self._remove_policy_routing(self.settings.fwmark, self.settings.table)
        lan = net24(self.settings.mgmt_ip)          # drop the segment→LAN forward accepts too
        if lan:
            for r in _lan_forward_rules(self.settings.segment_iface, self.settings.mgmt_iface, lan):
                self._run_ok(["iptables", "-D", *r])
        if failed:
            return NetResult(ok=False, error="teardown incomplete: " + "; ".join(failed))
        return NetResult(ok=True)

    def _del_table(self, failed: list[str], family: str) -> None:
        """Delete an nft table; 'No such file' (already gone) is fine, any other error is recorded."""
        try:
            self._run(["nft", "delete", "table", family, NFT_TABLE])
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or "").lower()
            if "no such" not in err and "does not exist" not in err:
                failed.append(f"{family} table: {(exc.stderr or str(exc)).strip()}")
        except OSError as exc:
            failed.append(f"{family} table: {exc}")

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
        """Run ignoring a non-zero exit or a missing binary (the rule/table is already absent,
        or the tool isn't installed on this host) — this is the idempotent best-effort path."""
        try:
            self._run(cmd)
        except (subprocess.CalledProcessError, OSError):
            pass

    def _ensure_forward(self, ipv6: bool = False) -> str:
        """Ensure IPv4 (and, when tunnelling v6, IPv6) forwarding. Returns a warning string when a
        write failed or didn't take — with forwarding off, all forwarded client traffic is dropped
        even though the nft/tproxy apply otherwise 'succeeded', so the operator must be told."""
        if not self._forward_on("/proc/sys/net/ipv4/ip_forward"):
            return "ip_forward could not be enabled — forwarded client traffic will be dropped"
        if ipv6:
            self._write_proc("/proc/sys/net/ipv6/conf/all/forwarding", "1")
            # D: enabling v6 forwarding makes the kernel stop accepting RAs by default, so the
            # Pi's Home leg can lose its own v6 (address + default route → no v6 egress).
            # accept_ra=2 keeps accepting RA on the uplink even while forwarding.
            self._write_proc(f"/proc/sys/net/ipv6/conf/{self.settings.mgmt_iface}/accept_ra", "2")
        return ""

    def _forward_on(self, path: str) -> bool:
        """Write '1' then read back to confirm it took (a swallowed write failure would otherwise
        report success while the segment is dead). Read-back is the source of truth so an injected
        write_proc (tests) that returns None still works."""
        self._write_proc(path, "1")
        try:
            with open(path) as f:
                return f.read().strip() == "1"
        except OSError:
            return True   # can't read back (dev / injected write_proc) — trust the write

    def _apply_lan_access(self, plan: NetPlan) -> str:
        """Let the segment reach the home LAN: (re)insert the forward-accepts into Docker's
        DOCKER-USER chain (the masquerade itself rides the panel's own nft table, rendered above).
        Idempotent — delete any prior copy, then insert only when lan_access is on. The delete is
        best-effort (stale copy may be absent); an insert failure is surfaced as a warning (LAN
        access is a secondary feature — it must not fail the whole tunnel apply, but the operator
        should know it didn't take). Scoped to the LAN /24 — never a WAN path. Returns a warning."""
        lan = net24(plan.mgmt_ip)
        if not lan or not plan.segment_iface or not plan.mgmt_iface:
            return ""
        rules = _lan_forward_rules(plan.segment_iface, plan.mgmt_iface, lan)
        for r in rules:
            self._run_ok(["iptables", "-D", *r])        # clear any stale copy (idempotent, best-effort)
        if plan.lan_access:
            for r in rules:
                try:
                    self._run(["iptables", "-I", *r])   # (re)insert at the top of DOCKER-USER
                except (subprocess.CalledProcessError, OSError) as exc:
                    return f"LAN access rule not applied: {(getattr(exc, 'stderr', None) or str(exc)).strip()}"
        return ""
