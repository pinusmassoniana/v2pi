import json
import logging
import os
import subprocess

from pi_gw_panel.config import safe_int
from pi_gw_panel.net_control.plan import NetPlan, NetResult, net24
from pi_gw_panel.net_control.render import render_nft, render_nft6

_log = logging.getLogger("pi_gw_panel")

NFT_TABLE = "pi_gw_panel"
IPTABLES_CHAIN = "PI_GW_PANEL"

# --- host-command time limits --------------------------------------------------
# Every shell-out here (and in `provision`, which imports this runner) happens while the
# caller holds the apply-lock and, through the surrounding transaction, the process-wide DB
# lock. A command that never returns therefore does not merely fail one apply — it wedges
# every DB-touching request in the process (status, the health monitor, the traffic recorder)
# for as long as it hangs. So no host command may run unbounded.
#
# nft / ip / iptables are netlink round-trips that complete in milliseconds even on a loaded
# small gateway; 10s is ~100x the realistic worst case, so the cap only fires on a genuinely
# stuck kernel/netlink operation and never on a slow-but-working apply.
HOST_CMD_TIMEOUT = max(1, safe_int(os.environ.get("PI_GW_NET_CMD_TIMEOUT", "10"), 10,
                                   "PI_GW_NET_CMD_TIMEOUT"))
# `nsenter -t 1 -- systemctl is-active` / `nsenter -t 1 -- nmcli general reload` re-enter pid
# 1's namespaces and then talk D-Bus to systemd/NetworkManager, which is legitimately slower
# and whose own internal timeouts top out around 30s. 60s therefore only fires when the tool
# is wedged rather than merely slow — a lower value would break real applies on small hardware.
SLOW_CMD_TIMEOUT = max(1, safe_int(os.environ.get("PI_GW_NET_SLOW_CMD_TIMEOUT", "60"), 60,
                                   "PI_GW_NET_SLOW_CMD_TIMEOUT"))
_SLOW_PROGRAMS = frozenset({"nsenter", "systemctl", "nmcli"})
# GNU `timeout(1)`'s exit status for "killed after the time limit" — a returncode no host tool
# used here produces on its own, so a timeout stays identifiable in logs.
TIMEOUT_RETURNCODE = 124


def command_timeout(cmd: list[str]) -> int:
    """Seconds allowed for `cmd`, chosen by command class (see the constants above)."""
    program = os.path.basename(cmd[0]) if cmd else ""
    return SLOW_CMD_TIMEOUT if program in _SLOW_PROGRAMS else HOST_CMD_TIMEOUT


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


def _run(cmd: list[str], input: str | None = None,
         timeout: float | None = None) -> subprocess.CompletedProcess:
    """Default runner: subprocess.run with check; raises CalledProcessError on non-zero.

    Bounded by `command_timeout(cmd)` unless a limit is passed explicitly. Exceeding it is
    re-raised as a `CalledProcessError` rather than surfacing `TimeoutExpired`: every caller
    here (and in `provision`) already handles exactly `CalledProcessError`/`OSError`, so a hung
    command becomes the same *kind* of failure as a non-zero exit and travels the existing
    paths — the recovery strings, and the fail-closed guard reinstated by `_failed_apply`.
    Leaking a third exception type would instead escape those handlers.

    The child's partial output is deliberately NOT carried into the synthetic stderr:
    `_is_absent_error` classifies a failure from its stderr text, so a half-written
    "No such file" from a command that then hung must never be read as "already absent".
    """
    limit = command_timeout(cmd) if timeout is None else timeout
    try:
        return subprocess.run(cmd, input=input, capture_output=True, text=True,
                              check=True, timeout=limit)
    except subprocess.TimeoutExpired as exc:
        _log.error("host command timed out after %ss: %s", limit, " ".join(cmd))
        raise subprocess.CalledProcessError(
            TIMEOUT_RETURNCODE, cmd, output="",
            stderr=f"command timed out after {limit}s: {' '.join(cmd)}") from exc


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
        self._verify_proc_writes = write_proc is None

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
            lan_warning = self._apply_lan_access(plan)
            self._verify_tproxy(plan)
            return NetResult(ok=True, rendered=nft_text, warning=lan_warning)
        except (subprocess.CalledProcessError, OSError) as exc:
            return self._failed_apply(
                plan, nft_text, (getattr(exc, "stderr", None) or str(exc)).strip())
        except RuntimeError as exc:
            return self._failed_apply(plan, nft_text, str(exc))

    def _failed_apply(self, plan: NetPlan, nft_text: str, error: str) -> NetResult:
        """Roll a half-applied tproxy back to the fail-closed guard before reporting failure.

        `nft -f` loads the tproxy ruleset first, so a failure in the policy-routing/forwarding
        steps after it would otherwise leave the segment tproxy'd to a route that does not
        exist — a black hole no caller repairs (the recovery path just re-runs the same apply).
        The guard is the same ruleset without tproxy: kill-switch drop when it is on, an empty
        table when it is off, and never any policy routing.
        """
        guard = self.apply_guard(plan)
        if not guard.ok:
            error = f"{error}; fail-closed fallback also failed: {guard.error}"
        return NetResult(ok=False, rendered=nft_text, error=error)

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
            # LAN access is independent of tunnel state; it is also secondary, so a failure to
            # place its rules is a warning — it must never fail the enforcement apply.
            lan_warning = self._apply_lan_access(plan)
            self._verify_table("ip")
            if plan.kill_switch:
                self._verify_table("ip6")
            return NetResult(ok=True, rendered=nft_text, warning=lan_warning)
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
        """Undo exactly what `apply_tproxy` installed.

        The policy table is not necessarily ours alone — an operator or another daemon may keep
        routes there — so the single `local default dev lo` route we added is deleted by name
        rather than flushing the whole table out from under them.
        """
        failed: list[str] = []
        fw, tbl = f"0x{fwmark:x}", str(table)
        self._cleanup_command(
            ["ip", "rule", "del", "fwmark", fw, "lookup", tbl], failed, "IPv4 rule")
        self._cleanup_command(
            ["ip", "route", "del", "local", "default", "dev", "lo", "table", tbl],
            failed, "IPv4 route")
        failed.extend(self._remove_v6_policy_routing(fwmark, table))
        return failed

    def _remove_v6_policy_routing(self, fwmark: int, table: int) -> list[str]:
        failed: list[str] = []
        fw, tbl = f"0x{fwmark:x}", str(table)
        self._cleanup_command(
            ["ip", "-6", "rule", "del", "fwmark", fw, "lookup", tbl], failed,
            "IPv6 rule")
        self._cleanup_command(
            ["ip", "-6", "route", "del", "local", "default", "dev", "lo", "table", tbl],
            failed, "IPv6 route")
        return failed

    def _run_ok(self, cmd: list[str]) -> None:
        """Run ignoring a non-zero exit or a missing binary (the rule/table is already absent,
        or the tool isn't installed on this host) — this is the idempotent best-effort path."""
        try:
            self._run(cmd)
        except (subprocess.CalledProcessError, OSError):
            pass

    def _ensure_forward(self, ipv6: bool = False) -> str:
        """Ensure IPv4 (and, when tunnelling v6, IPv6) forwarding. Returns an error string when a
        write failed or didn't take — with forwarding off, all forwarded client traffic is dropped,
        so the segment is dead and the apply has not really succeeded (unlike LAN access, which is
        secondary and only warns)."""
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
        report success while the segment is dead). Production writes are read back; injected test
        writers are trusted."""
        return self._proc_value(path, "1")

    def _proc_value(self, path: str, value: str) -> bool:
        wrote = self._write_proc(path, value)
        if wrote is False:
            return False
        if not self._verify_proc_writes:
            return True
        try:
            with open(path) as f:
                return f.read().strip() == value
        except OSError:
            return True   # can't read back (dev / injected write_proc) — trust the write

    def _apply_lan_access(self, plan: NetPlan) -> str:
        """Let the segment reach the home LAN: (re)insert the forward-accepts into Docker's
        DOCKER-USER chain (the masquerade itself rides the panel's own nft table, rendered above).
        Idempotent — delete any prior copy, then insert. Scoped to the LAN /24 — never a WAN path.

        With `lan_access` off this only tears the panel's chain down, entirely best-effort: it
        must never touch DOCKER-USER in a way that can fail, because a host with no Docker has
        no such chain and would otherwise be unable to bring the tunnel up at all.

        Returns a WARNING string, never an error: LAN access is a secondary feature, so the
        operator is told it didn't take while enforcement itself still applies.
        """
        lan = net24(plan.mgmt_ip)
        if not lan or not plan.segment_iface or not plan.mgmt_iface:
            return ""
        if not plan.lan_access:
            self._remove_lan_chain([])      # best-effort; a missing DOCKER-USER is fine here
            return ""
        rules = _lan_forward_rules(plan.segment_iface, plan.mgmt_iface, lan)
        try:
            # The chain may already exist. The following flush is authoritative and will still
            # fail if creation was denied, so ignoring only this idempotent create is safe.
            self._run_ok(["iptables", "-N", IPTABLES_CHAIN])
            self._run_ok(["iptables", "-D", "DOCKER-USER", "-j", IPTABLES_CHAIN])
            self._run(["iptables", "-I", "DOCKER-USER", "1", "-j", IPTABLES_CHAIN])
            self._run(["iptables", "-F", IPTABLES_CHAIN])
            for rule in rules:
                self._run(["iptables", "-A", *rule])
        except (subprocess.CalledProcessError, OSError) as exc:
            warning = ("LAN access chain not applied: "
                       f"{(getattr(exc, 'stderr', None) or str(exc)).strip()}")
            _log.warning("%s", warning)
            return warning
        return ""

    def _remove_lan_chain(self, failed: list[str]) -> None:
        self._cleanup_command(
            ["iptables", "-D", "DOCKER-USER", "-j", IPTABLES_CHAIN], failed,
            "iptables jump")
        self._cleanup_command(["iptables", "-F", IPTABLES_CHAIN], failed, "iptables flush")
        self._cleanup_command(["iptables", "-X", IPTABLES_CHAIN], failed, "iptables chain")

    def _verify_table(self, family: str) -> None:
        self._run(["nft", "list", "table", family, NFT_TABLE])

    def _json(self, cmd: list[str]) -> list[dict]:
        """Structured `ip -j …` read-back. Anything unparseable reads as 'nothing found', so a
        verification built on it fails closed instead of matching stray text."""
        out = (self._run(cmd).stdout or "").strip()
        try:
            data = json.loads(out) if out else []
        except ValueError:
            return []
        return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []

    @staticmethod
    def _same_mark(value, fwmark: int) -> bool:
        """`ip -j rule show` reports fwmark as '0x40' (or, on some builds, an int)."""
        if isinstance(value, bool) or value is None:
            return False
        if isinstance(value, int):
            return value == fwmark
        try:
            return int(str(value), 0) == fwmark
        except ValueError:
            return False

    def _has_fwmark_rule(self, entries: list[dict], fwmark: int, table: int) -> bool:
        return any(self._same_mark(e.get("fwmark"), fwmark) and str(e.get("table")) == str(table)
                   for e in entries)

    @staticmethod
    def _has_local_lo_route(entries: list[dict]) -> bool:
        return any(e.get("type") == "local" and e.get("dst") == "default" and e.get("dev") == "lo"
                   for e in entries)

    def _verify_tproxy(self, plan: NetPlan) -> None:
        """Bounded kernel read-back after a mutation; absence is a failed apply, not a warning.

        Matched structurally against `ip -j` output: a substring search for the table number
        hits any unrelated rule that happens to contain those digits (and `"lo" in routes` is
        satisfied by the word 'local'), so a text match can call an unapplied ruleset verified.
        """
        self._verify_table("ip")
        self._verify_table("ip6")
        fw, tbl = f"0x{plan.fwmark:x}", str(plan.table)
        families = [("IPv4", [])] + ([("IPv6", ["-6"])] if plan.ipv6_enabled else [])
        for label, flag in families:
            rules = self._json(["ip", *flag, "-j", "rule", "show"])
            routes = self._json(["ip", *flag, "-j", "route", "show", "table", tbl])
            if not self._has_fwmark_rule(rules, plan.fwmark, plan.table):
                raise RuntimeError(f"post-apply verification missing {label} fwmark {fw} rule")
            if not self._has_local_lo_route(routes):
                raise RuntimeError(
                    f"post-apply verification missing {label} local route table {tbl}")
