import subprocess
from pi_gw_panel.net_control.plan import NetPlan, NetResult, net24
from pi_gw_panel.net_control.render import render_nft, render_nft6

NFT_TABLE = "pi_gw_panel"
IPTABLES_CHAIN = "PI_GW_PANEL"


def _lan_forward_rules(seg_if: str, lan_if: str, lan_cidr: str) -> list[list[str]]:
    """Rules owned inside the panel's stable iptables chain.

    Flushing one private chain removes stale interface/CIDR tuples after settings changes; direct
    DOCKER-USER rules cannot be found reliably once their source settings have changed.
    """
    return [
        [IPTABLES_CHAIN, "-i", seg_if, "-o", lan_if, "-d", lan_cidr, "-j", "ACCEPT"],
        [IPTABLES_CHAIN, "-i", lan_if, "-o", seg_if,
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
            self._delete_or_absent(["ip", "rule", "del", "fwmark", fw, "lookup", tbl])
            self._run(["ip", "rule", "add", "fwmark", fw, "lookup", tbl])
            self._run(["ip", "route", "replace", "local", "default", "dev", "lo", "table", tbl])
            if plan.ipv6_enabled:           # mirror the policy routing for v6 tproxy
                self._delete_or_absent(
                    ["ip", "-6", "rule", "del", "fwmark", fw, "lookup", tbl])
                self._run(["ip", "-6", "rule", "add", "fwmark", fw, "lookup", tbl])
                self._run(["ip", "-6", "route", "replace", "local", "default", "dev", "lo", "table", tbl])
            else:                            # E: drop any stale v6 routing left from a prior v6 on
                failures = self._remove_v6_policy_routing(plan.fwmark, plan.table)
                if failures:
                    raise RuntimeError("; ".join(failures))
            forwarding_error = self._ensure_forward(ipv6=plan.ipv6_enabled)
            if forwarding_error:
                raise RuntimeError(forwarding_error)
            lan_error = self._apply_lan_access(plan)
            if lan_error:
                raise RuntimeError(lan_error)
            self._verify_tproxy(plan)
            return NetResult(ok=True, rendered=nft_text)
        except (subprocess.CalledProcessError, OSError) as exc:
            return NetResult(ok=False, rendered=nft_text,
                             error=(getattr(exc, "stderr", None) or str(exc)).strip())
        except RuntimeError as exc:
            return NetResult(ok=False, rendered=nft_text, error=str(exc))

    def apply_guard(self, plan: NetPlan) -> NetResult:
        """Fail-closed leak-guard (A1): install the kill-switch drop (v4 + v6) with NO
        tproxy/policy-routing — for when the tunnel is intentionally stopped but the
        kill-switch must keep blocking client→WAN. With the kill-switch off this is an
        empty table (effectively a teardown of the tproxy rules)."""
        nft_text = self._nft_script(plan, tunnel_up=False)
        try:
            self._run(["nft", "-f", "-"], input=nft_text)
            failures = self._remove_policy_routing(plan.fwmark, plan.table)
            if failures:
                raise RuntimeError("; ".join(failures))
            lan_error = self._apply_lan_access(plan)  # LAN access is independent of tunnel state
            if lan_error:
                raise RuntimeError(lan_error)
            self._verify_table("ip")
            if plan.kill_switch:
                self._verify_table("ip6")
            return NetResult(ok=True, rendered=nft_text)
        except (subprocess.CalledProcessError, OSError) as exc:
            return NetResult(ok=False, rendered=nft_text,
                             error=(getattr(exc, "stderr", None) or str(exc)).strip())
        except RuntimeError as exc:
            return NetResult(ok=False, rendered=nft_text, error=str(exc))

    def teardown(self) -> NetResult:
        """Best-effort remove (ignore-if-absent) — the rollback + no-kill-switch stop path.
        Distinguishes 'already absent' (fine) from a command that actually failed, so the caller
        isn't told the rollback succeeded while stale rules (e.g. a kill-switch drop) remain."""
        failed: list[str] = []
        self._del_table(failed, "ip")
        self._del_table(failed, "ip6")
        failed.extend(self._remove_policy_routing(self.settings.fwmark, self.settings.table))
        self._remove_lan_chain(failed)
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

    @staticmethod
    def _is_absent_error(exc: subprocess.CalledProcessError) -> bool:
        err = ((exc.stderr or "") + " " + (exc.stdout or "")).lower()
        return any(token in err for token in (
            "no such", "does not exist", "not found", "cannot find", "bad rule",
            "no chain/target/match"))

    def _delete_or_absent(self, cmd: list[str]) -> None:
        try:
            self._run(cmd)
        except subprocess.CalledProcessError as exc:
            if not self._is_absent_error(exc):
                raise

    def _cleanup_command(self, cmd: list[str], failed: list[str], label: str) -> None:
        try:
            self._run(cmd)
        except subprocess.CalledProcessError as exc:
            if not self._is_absent_error(exc):
                failed.append(f"{label}: {(exc.stderr or str(exc)).strip()}")
        except OSError as exc:
            failed.append(f"{label}: {exc}")

    def _remove_policy_routing(self, fwmark: int, table: int) -> list[str]:
        failed: list[str] = []
        fw, tbl = f"0x{fwmark:x}", str(table)
        self._cleanup_command(
            ["ip", "rule", "del", "fwmark", fw, "lookup", tbl], failed, "IPv4 rule")
        self._cleanup_command(["ip", "route", "flush", "table", tbl], failed, "IPv4 route")
        failed.extend(self._remove_v6_policy_routing(fwmark, table))
        return failed

    def _remove_v6_policy_routing(self, fwmark: int, table: int) -> list[str]:
        failed: list[str] = []
        fw, tbl = f"0x{fwmark:x}", str(table)
        self._cleanup_command(
            ["ip", "-6", "rule", "del", "fwmark", fw, "lookup", tbl], failed,
            "IPv6 rule")
        self._cleanup_command(
            ["ip", "-6", "route", "flush", "table", tbl], failed, "IPv6 route")
        return failed

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
            if not self._proc_value("/proc/sys/net/ipv6/conf/all/forwarding", "1"):
                return "IPv6 forwarding could not be enabled"
            # D: enabling v6 forwarding makes the kernel stop accepting RAs by default, so the
            # Pi's Home leg can lose its own v6 (address + default route → no v6 egress).
            # accept_ra=2 keeps accepting RA on the uplink even while forwarding.
            if not self._proc_value(
                    f"/proc/sys/net/ipv6/conf/{self.settings.mgmt_iface}/accept_ra", "2"):
                return "IPv6 uplink accept_ra=2 could not be enabled"
        return ""

    def _forward_on(self, path: str) -> bool:
        """Write '1' then read back to confirm it took (a swallowed write failure would otherwise
        report success while the segment is dead). Read-back is the source of truth so an injected
        write_proc (tests) that returns None still works."""
        return self._proc_value(path, "1")

    def _proc_value(self, path: str, value: str) -> bool:
        wrote = self._write_proc(path, value)
        if wrote is False:
            return False
        try:
            with open(path) as f:
                return f.read().strip() == value
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
        try:
            # The chain may already exist. The following flush is authoritative and will still
            # fail if creation was denied, so ignoring only this idempotent create is safe.
            self._run_ok(["iptables", "-N", IPTABLES_CHAIN])
            self._run_ok(["iptables", "-D", "DOCKER-USER", "-j", IPTABLES_CHAIN])
            self._run(["iptables", "-I", "DOCKER-USER", "1", "-j", IPTABLES_CHAIN])
            self._run(["iptables", "-F", IPTABLES_CHAIN])
            if plan.lan_access:
                for rule in rules:
                    self._run(["iptables", "-A", *rule])
        except (subprocess.CalledProcessError, OSError) as exc:
            return f"LAN access chain not applied: {(getattr(exc, 'stderr', None) or str(exc)).strip()}"
        return ""

    def _remove_lan_chain(self, failed: list[str]) -> None:
        self._cleanup_command(
            ["iptables", "-D", "DOCKER-USER", "-j", IPTABLES_CHAIN], failed,
            "iptables jump")
        self._cleanup_command(["iptables", "-F", IPTABLES_CHAIN], failed, "iptables flush")
        self._cleanup_command(["iptables", "-X", IPTABLES_CHAIN], failed, "iptables chain")

    def _verify_table(self, family: str) -> None:
        self._run(["nft", "list", "table", family, NFT_TABLE])

    def _verify_tproxy(self, plan: NetPlan) -> None:
        """Bounded kernel read-back after a mutation; absence is a failed apply, not a warning."""
        self._verify_table("ip")
        self._verify_table("ip6")
        fw, tbl = f"0x{plan.fwmark:x}", str(plan.table)
        rules = self._run(["ip", "rule", "show"]).stdout or ""
        routes = self._run(["ip", "route", "show", "table", tbl]).stdout or ""
        if fw.lower() not in rules.lower() or tbl not in rules:
            raise RuntimeError(f"post-apply verification missing IPv4 fwmark {fw} rule")
        if "local" not in routes or "lo" not in routes:
            raise RuntimeError(f"post-apply verification missing IPv4 local route table {tbl}")
        if plan.ipv6_enabled:
            rules6 = self._run(["ip", "-6", "rule", "show"]).stdout or ""
            routes6 = self._run(["ip", "-6", "route", "show", "table", tbl]).stdout or ""
            if fw.lower() not in rules6.lower() or tbl not in rules6:
                raise RuntimeError(f"post-apply verification missing IPv6 fwmark {fw} rule")
            if "local" not in routes6 or "lo" not in routes6:
                raise RuntimeError(f"post-apply verification missing IPv6 local route table {tbl}")
