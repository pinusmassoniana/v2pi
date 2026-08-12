"""Self-provisioning gateway: bring the whole host gateway up from settings, idempotently.

Gated on the linux backend (a net backend carrying a `_run` seam) + the `manage_segment`
setting. Every side-effect goes through an injectable seam — `run` for shell-outs,
`write_proc`/`write_file` for /proc + conf files — so the command/file emission is unit-tested
with no root or Pi. The default runner/proc-writer ARE the LinuxBackend ones (imported, not
re-declared) so both paths keep one contract; the real apply passes the backend's own seam.

That shared runner is also where the per-command time limit lives, so every shell-out below is
bounded (`linux.command_timeout`) — including the `nsenter`-into-pid-1 calls, which get the
slower class. This matters more here than anywhere else: `host_provision` and the PD watcher
callback run these under the controller apply-lock, which the DB transactions around them turn
into a process-wide mutex, so an unbounded `nmcli` would stall every request in the panel.
A command that exceeds its limit raises `CalledProcessError`, which the handlers below already
catch, so there is no separate timeout path to route. There is still one to READ, though: that
exception carries `TIMEOUT_RETURNCODE` and a synthetic stderr about the command, and says nothing
about the host state the command was inspecting — so anything below that infers a fact from a
failure has to exclude it explicitly (see `_probe_link`)."""
import ipaddress
import json
import logging
import os
import secrets
import subprocess
from dataclasses import dataclass, field

from pi_gw_panel.net_control.linux import TIMEOUT_RETURNCODE, _run, _write_proc
from pi_gw_panel.net_control.plan import NetPlan, NetResult
from pi_gw_panel.net_control.render import render_dnsmasq

_log = logging.getLogger("pi_gw_panel")

NM_CONF_PATH = "/etc/NetworkManager/conf.d/99-v2pi.conf"


def _write_file(path: str, text: str) -> None:
    try:
        with open(path, "w") as f:
            f.write(text)
    except OSError:
        pass


def _remove_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# --- pure helpers --------------------------------------------------------------

def parse_vlan(iface: str) -> tuple[str, int | None]:
    """`eth0.2` -> (`eth0`, 2); a dotless iface -> (iface, None) (no VLAN to create)."""
    if "." in iface:
        parent, vid = iface.rsplit(".", 1)
        try:
            return parent, int(vid)
        except ValueError:
            return iface, None
    return iface, None


def host_addr6(segment_ip6: str) -> str | None:
    """The gateway's own v6 address in the segment /64 = first host address (`<prefix>::1/64`).
    None for blank / `auto` / invalid."""
    s = (segment_ip6 or "").strip()
    if not s or s.lower() == "auto":
        return None
    try:
        net = ipaddress.ip_network(s, strict=False)
    except ValueError:
        return None
    if net.version != 6 or net.prefixlen != 64:
        return None
    return f"{net.network_address + 1}/64"


def generate_ula_prefix(vlan_id: int, rand=secrets.token_bytes) -> str:
    """A stable, install-unique ULA /64: `fd` + 40 random bits (global ID) + 16-bit subnet =
    the VLAN id. Persisted by the caller so it survives reboots."""
    gid = rand(5)
    b = bytes([0xFD]) + gid + bytes([(vlan_id >> 8) & 0xFF, vlan_id & 0xFF]) + bytes(8)
    net = ipaddress.ip_network((ipaddress.IPv6Address(b), 64), strict=False)
    return net.with_prefixlen


# --- host bring-up steps -------------------------------------------------------

# `ip link show` has THREE possible answers, and only two of them are facts about the link. It
# can report the link, it can report that no such device exists, or it can fail to answer at all
# — refused, a netlink error, or cut short by the runner's time limit, which arrives as
# `CalledProcessError(TIMEOUT_RETURNCODE)` carrying nothing about the link whatsoever. Collapsing
# the third answer into "not there" is safe for the bring-up question below and NOT safe for the
# ownership question (`_delete_owned_link`), where it silently licenses forgetting a live link, so
# the probe states are kept apart here and each caller collapses them its own way, deliberately.
LINK_PRESENT = "present"
LINK_ABSENT = "absent"
LINK_UNKNOWN = "unknown"

# What iproute2 says when the device genuinely is not there. Same shape, and the same reasoning,
# as the linux backend's `_is_absent_error`: absence is proven by an explicit not-found text and
# by nothing else, so every unrecognised failure stays unknown.
_LINK_ABSENT_TOKENS = ("does not exist", "cannot find", "no such")


def _probe_link(iface: str, run=_run) -> tuple[str, str]:
    """Ask the host whether `iface` is on it. `(state, reason)`; `reason` explains an unknown.

    `LINK_ABSENT` is returned only for an explicit not-found answer. A time limit is never one:
    the synthetic stderr the runner raises describes the command it killed, not the device, and
    the kill says nothing about whether the link survived — so it, EPERM, and any unexpected exit
    are all `LINK_UNKNOWN`. The returncode is checked as well as the text, because a command that
    printed something and then hung must not have its half-written output read as an answer.
    """
    try:
        run(["ip", "link", "show", iface])
        return LINK_PRESENT, ""
    except subprocess.CalledProcessError as exc:
        err = ((exc.stderr or "") + " " + (exc.stdout or "")).lower()
        if exc.returncode != TIMEOUT_RETURNCODE and any(t in err for t in _LINK_ABSENT_TOKENS):
            return LINK_ABSENT, ""
        reason = (exc.stderr or "").strip() or f"ip link show exited {exc.returncode}"
        return LINK_UNKNOWN, reason
    except OSError as exc:
        return LINK_UNKNOWN, str(exc) or exc.__class__.__name__


def _link_exists(iface: str, run=_run) -> bool:
    """Yes/no for the bring-up decision: is `iface` already on the host?

    Present is True, and BOTH "not there" and "could not tell" are False — the contract this
    seam has always had, and the right collapse for this question: the answer decides whether to
    create the VLAN, an `ip link add` the kernel refuses because the link does exist raises and
    is reported, whereas skipping the add on an unanswered probe would leave the segment with no
    link at all. The ownership question runs the other way and must not use this — see
    `_delete_owned_link`, which reads `_probe_link` directly.
    """
    return _probe_link(iface, run)[0] == LINK_PRESENT


# An address is asked about in the SAME three-valued vocabulary as a link, and for the same
# reason: only an explicit answer from the host is an answer, and a probe that could not answer
# proves nothing at all. One vocabulary and one set of values, not two — a recorded state reads
# back by the same names whichever kind of host object it describes, and the two can never drift
# apart into subtly different meanings.
#
# It is asked in exactly one place: after an `ip addr del` the panel issued against an address it
# OWNS (the reconcile/clear paths) failed. There `ADDR_ABSENT` is what licenses forgetting the
# ownership record — the address is gone, so there is nothing left to retry — and every other
# answer keeps it. The undo path asks nothing: it no longer deletes addresses at all.
ADDR_PRESENT, ADDR_ABSENT, ADDR_UNKNOWN = LINK_PRESENT, LINK_ABSENT, LINK_UNKNOWN


def _run_stdout(result) -> str | None:
    """The text a `run` seam produced, or None when it produced nothing that can be READ.

    The production runner returns `CompletedProcess`; a backend's own seam (and the fakes) may
    return the string directly. Anything else — a `CompletedProcess` whose `stdout` is None
    (capture off), a seam that returns None, a mock — is not output. It is the ABSENCE of output,
    and the two are different facts: "the interface listed nothing" is an answer about the
    interface, "there is nothing to read" is an answer about the probe.

    Collapsing the second into the first read as `ADDR_ABSENT`, which is the one answer that says
    a failed removal has nothing left to retry, and so the one answer that licenses dropping the
    ownership record for an address the panel put on the host. An unreadable probe would therefore
    have retired the record that would have retried the removal, leaving the address stranded. So
    it returns None, and `_probe_addr` turns that into `ADDR_UNKNOWN`: demonstrably a string, or
    no answer at all.
    """
    text = getattr(result, "stdout", result)
    return text if isinstance(text, str) else None


def _probe_addr(iface: str, addr: str, run=_run) -> tuple[str, str]:
    """Ask the host whether `addr` is on `iface`. `(state, reason)`; `reason` explains an unknown.

    The CIDR is matched as a whole token, exactly as `ip addr del` matches it — address AND
    prefix length — so a `192.168.10.2/24` whose record this would drop is never answered for by
    a `192.168.10.2/16` some other owner put there.

    A device that is not on the host carries no addresses, so an explicit not-found answers
    `ADDR_ABSENT`. Everything else — refused, a netlink error, the runner's time limit (whose
    synthetic stderr describes the command it killed and says nothing about the interface), or a
    command that exited cleanly and produced no readable output at all — is `ADDR_UNKNOWN`.
    Never absence: absence is the one answer that lets an ownership record be forgotten.
    """
    if not iface or not addr:
        return ADDR_UNKNOWN, "no interface or address"
    family = "-6" if ":" in addr else "-4"
    try:
        out = _run_stdout(run(["ip", family, "-o", "addr", "show", "dev", iface]))
    except subprocess.CalledProcessError as exc:
        err = ((exc.stderr or "") + " " + (exc.stdout or "")).lower()
        if exc.returncode != TIMEOUT_RETURNCODE and any(t in err for t in _LINK_ABSENT_TOKENS):
            return ADDR_ABSENT, ""
        reason = (exc.stderr or "").strip() or f"ip addr show exited {exc.returncode}"
        return ADDR_UNKNOWN, reason
    except OSError as exc:
        return ADDR_UNKNOWN, str(exc) or exc.__class__.__name__
    if out is None:                     # it exited fine and said nothing we can read
        return ADDR_UNKNOWN, "ip addr show produced no readable output"
    return (ADDR_PRESENT if addr in out.split() else ADDR_ABSENT), ""


def _nm_active(run=_run) -> bool:
    """True if a NetworkManager is running on the host (so a reload is meaningful)."""
    try:
        run(["nsenter", "-t", "1", "-m", "-n", "--",
             "systemctl", "is-active", "--quiet", "NetworkManager"])
        return True
    except (subprocess.CalledProcessError, OSError):
        return False


def ensure_sysctls(settings, write_proc=_write_proc) -> None:
    """Forwarding (v4 + v6) and accept_ra=2 on the uplink (so the Pi keeps its own v6 default
    route even with forwarding on). Best-effort; the privileged container has writable
    /proc/sys."""
    write_proc("/proc/sys/net/ipv4/ip_forward", "1")
    write_proc("/proc/sys/net/ipv6/conf/all/forwarding", "1")
    write_proc(f"/proc/sys/net/ipv6/conf/{settings.mgmt_iface}/accept_ra", "2")


LINK_KEY = "managed_segment_link"


def _parse_links(store) -> list[str]:
    """Interface names recorded as VLAN links this panel created, oldest first.

    Newline-joined, the same encoding the stale-address ledger uses. A gateway upgrading from
    a release that stored a single bare name here reads back as a one-entry list, so the link
    it already owns stays recognised and cleanable instead of being orphaned by the format.
    """
    out: list[str] = []
    for line in (store.get_setting(LINK_KEY) or "").splitlines():
        name = line.strip()
        if name and name not in out:
            out.append(name)
    return out


def _record_links(store, links: list[str]) -> None:
    store.set_setting(LINK_KEY, "\n".join(links))


def _probe_seam(link_exists, run):
    """The three-state probe the ownership path needs, from whatever the caller supplied.

    Production supplies nothing and gets `_probe_link`, which can answer "could not tell" and is
    the whole point: this is the only path where that answer is load-bearing. A caller injecting
    a bool `link_exists` is supplying its own model of the host, whose answer is authoritative by
    construction, so True/False map straight onto present/absent — the collapse that is only ever
    a lie for the real probe, which is why the real probe no longer performs it.

    The returned callable does not raise: a probe that cannot run has proven nothing, which is
    `LINK_UNKNOWN`, not absence, and one link that cannot be checked may not abort the pass that
    still has a segment to bring up.
    """
    inner = ((lambda iface: _probe_link(iface, run)) if link_exists is None
             else (lambda iface: (LINK_PRESENT if link_exists(iface) else LINK_ABSENT, "")))

    def probe(iface: str) -> tuple[str, str]:
        try:
            return inner(iface)
        except (subprocess.CalledProcessError, OSError) as exc:
            return LINK_UNKNOWN, str(exc) or exc.__class__.__name__
    return probe


def _delete_owned_link(store, link: str, owned: list[str], run, probe) -> str | None:
    """Remove one VLAN previously recorded as panel-created, then forget that one entry.

    The entry is dropped only once the link is PROVABLY gone, and a failed delete does not prove
    it: "the link was already gone" (reboot / manual cleanup) and "the removal was refused"
    — EPERM, `Device or resource busy`, or the runner's time limit — are indistinguishable from
    the exception alone. Clearing on any failure would recreate exactly the orphan this ledger
    exists to prevent, a link the panel put on the host, still up, with nothing recording that
    the panel owns it, reached through the error path.

    So absence is asked of the host, and only `LINK_ABSENT` clears the entry. `LINK_UNKNOWN` —
    a probe that timed out, was refused, or exited in a way that says nothing about the device —
    is treated exactly like "still there": the entry stays so a later pass retries the delete,
    and the reason names both halves, because a delete that failed AND a probe that could not
    answer is the state most likely to be sitting on a live VLAN.
    """
    try:
        run(["ip", "link", "delete", link])
    except (subprocess.CalledProcessError, OSError) as exc:
        state, detail = probe(link)
        if state != LINK_ABSENT:
            why = (getattr(exc, "stderr", "") or str(exc)).strip() or "delete failed"
            if state == LINK_UNKNOWN:
                why += ("; the link could not be probed afterwards, so it is still owned: "
                        + (detail or "no answer from ip link show"))
            return f"{link}: {why}"
    if link in owned:
        owned.remove(link)
    _record_links(store, owned)
    return None


def _retire_links(store, links: list[str], owned: list[str], run, probe) -> list[str]:
    """Delete each recorded link in turn; return a reason per link not proven gone afterwards."""
    failed: list[str] = []
    for link in links:
        reason = _delete_owned_link(store, link, owned, run, probe)
        if reason is not None:
            _log.warning("could not remove the panel-created VLAN link %s", reason)
            failed.append(reason)
    return failed


def ensure_segment_link(store, plan: NetPlan, run=_run, link_exists=None) -> list[str]:
    """Create the configured VLAN when needed, bring the segment link up, and retire the
    panel-created links the current plan no longer wants.

    A VLAN the panel creates is appended to the ownership ledger BEFORE the kernel call, so
    disabling `manage_segment` can delete exactly the links this panel added (and never a
    pre-existing one) even if the process dies between the record and the creation. The ledger
    is a list for the same reason the stale-address one is: retargeting the segment
    (`eth0.2` -> `eth0.9`) must not overwrite the record of the link still on the host, or
    nothing would ever delete it. Each superseded link is dropped from the ledger only once its
    removal is proven, and every link that could not be retired is returned as a reason string
    so the caller reports a provisioning failure rather than success over a link it owns and
    left running.
    """
    probe = _probe_seam(link_exists, run)
    link_exists = link_exists or (lambda i: _link_exists(i, run))
    seg = plan.segment_iface
    parent, vid = parse_vlan(seg)
    owned = _parse_links(store)
    if vid is not None and not link_exists(seg):
        if seg not in owned:
            owned.append(seg)
            _record_links(store, owned)
        run(["ip", "link", "add", "link", parent, "name", seg,
             "type", "vlan", "id", str(vid)])
    run(["ip", "link", "set", seg, "up"])
    return _retire_links(store, [name for name in owned if name != seg], owned, run, probe)


def clear_managed_link(store, run=_run, link_exists=None) -> list[str]:
    """Delete every VLAN link this panel created, forgetting each one its removal is proven.
    Returns a reason per link that is not proven gone from the host afterwards."""
    owned = _parse_links(store)
    if not owned:
        return []
    return _retire_links(store, list(owned), owned, run, _probe_seam(link_exists, run))


def _retire_owned_addr(iface: str, addr: str, run=_run) -> str | None:
    """Remove one address the panel OWNS; return why its record must be kept, or None to drop it.

    This is normal operation, not crash recovery: the addresses reaching here are the ones the
    reconcile path is replacing and the ones the clear path is dropping because segment management
    went off. The panel installed them and recorded them before it did, so deleting them is
    legitimate and stays.

    What is not legitimate is FORGETTING one whose deletion did not happen. EPERM, `Cannot assign
    requested address`, and the runner's time limit (`CalledProcessError(TIMEOUT_RETURNCODE)`,
    whose synthetic stderr names the command it killed) all leave the address exactly where it
    was; a caller that treated them as done cleared the one record that would have retried the
    removal, so the address stayed on the host with nothing pointing at it — and with segment
    management off, invisible to readiness too, whose address check is deliberately skipped there.

    So the record is dropped on exactly two answers: the kernel accepted the delete, or the host
    says afterwards the address is not on the interface (lost across a reboot — nothing to retry).
    Anything else keeps it, INCLUDING a probe that could not answer, which is the state most
    likely to be sitting on an address that is still there.

    Only `CalledProcessError` is caught, exactly as before: an `OSError` (no `ip` binary at all)
    is not one address failing and still travels out to the caller's own handler.
    """
    if not addr or not iface:
        return None
    cmd = ["ip"] + (["-6"] if ":" in addr else []) + ["addr", "del", addr, "dev", iface]
    try:
        run(cmd)
    except subprocess.CalledProcessError as exc:
        why = ((exc.stderr or "").strip() or str(exc) or
               f"ip addr del exited {exc.returncode}")
        state, detail = _probe_addr(iface, addr, run)
        if state == ADDR_ABSENT:         # it is not there: the removal has nothing left to do
            return None
        if state == ADDR_UNKNOWN:
            why += ("; the address could not be probed afterwards, so it is still owned: "
                    + (detail or "no answer from ip addr show"))
        return f"{addr} on {iface}: {why}"
    return None


STALE_KEY = "managed_segment_stale"

# The ledger is a retry list, and a retry list that only ever grows is its own failure mode. Every
# DISTINCT address whose removal is refused stays on it, and nothing about that needs an operator:
# a DHCPv6-PD prefix can renew repeatedly on its own, and each renewal that cannot retire the
# address it supersedes adds an entry. Unbounded, that grows the persisted state and the
# delete-plus-probe loop that runs under the apply lock on every pass, forever.
#
# So the ledger is capped, and what the cap refuses is the ROTATION — the change that would need a
# new entry — before anything is written or any address is touched. Never a record: dropping the
# oldest entry to make room would forget an address the panel installed and left on the host, which
# is precisely the stranding this ledger exists to prevent, and it would do it silently. Refusing
# costs the operator a configuration change they can retry once the backlog drains, and the reason
# says so through the pass result. A pass that is NOT rotating still runs in full, so it installs
# the desired address and retries the whole backlog: that is how the limit is escaped rather than
# wedged. `clear_managed_addresses` never rotates either — it adds at most the two addresses it
# already owns and only shrinks after that — so the persisted ledger cannot exceed LIMIT + 2 pairs.
STALE_LIMIT = 16


def _parse_stale(store) -> list[tuple[str, str]]:
    """`(iface, addr)` pairs recorded as panel-owned but not yet removed from the kernel."""
    out: list[tuple[str, str]] = []
    for line in (store.get_setting(STALE_KEY) or "").splitlines():
        parts = line.split()
        if len(parts) == 2:
            out.append((parts[0], parts[1]))
    return out


def _record_ownership(store, iface: str, addr4: str, addr6: str,
                      stale: list[tuple[str, str]]) -> None:
    store.set_setting("managed_segment_iface", iface)
    store.set_setting("managed_segment_addr4", addr4)
    store.set_setting("managed_segment_addr6", addr6)
    store.set_setting(STALE_KEY, "\n".join(f"{i} {a}" for i, a in stale))


def _desired_pairs(iface: str, *addrs: str) -> frozenset[tuple[str, str]]:
    """The `(iface, addr)` pairs the pass is INSTALLING, which are never candidates for removal.

    The stale ledger records addresses owed a removal, and the address a pass is putting on the
    interface is the one thing that may never be owed one. Coming back to an address whose earlier
    removal was refused is exactly that collision: `A` -> `B` leaves `A` on the ledger, then `B` ->
    `A` makes `A` desired again, and a retention mechanism that does not check would `ip addr
    replace` `A` and then dutifully `ip addr del` `A` — because the ledger still names it — clear
    the record, and report no failure at all. The interface is left with no address, silently,
    which on a live gateway is the operator's network down.

    So this set is subtracted in both places the stale set is handled: where it is RECORDED, so no
    persisted entry ever names an address the panel is required to have, and where it is RETIRED,
    so the deletion site refuses one whatever a caller hands it. Subtracting forgets nothing — a
    desired address is recorded as the segment's current one, under `managed_segment_addr4`/
    `_addr6`, which is a stronger claim than being owed a removal, not a weaker one.
    """
    return frozenset((iface, a) for a in addrs if iface and a)


def _distinct(pairs, drop=frozenset()) -> list[tuple[str, str]]:
    """`pairs` in order, without blanks, repeats, or anything in `drop`."""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pair in pairs:
        if not (pair[0] and pair[1]) or pair in seen or pair in drop:
            continue
        seen.add(pair)
        out.append(pair)
    return out


def _retire_owned(pairs: list[tuple[str, str]], run,
                  desired=frozenset()) -> tuple[list[tuple[str, str]], list[str]]:
    """Remove each panel-owned address in turn; return `(pairs to keep recorded, reasons)`.

    A pair whose removal is not proven stays in the ledger, so the next pass retries it, and its
    reason travels back to the caller — which reports it through the pass result. Nothing is
    forgotten on a failure: that is how a refused delete used to become an address on the host
    with no record of it anywhere.

    This is the module's only address-deletion site, which is why `desired` is enforced here too
    and not only where the ledger is written (see `_desired_pairs`): a pair the pass is installing
    is skipped, whatever the ledger or the caller says. The clear path passes none, correctly — it
    runs only with segment management off, where the panel desires no address at all.
    """
    keep: list[tuple[str, str]] = []
    reasons: list[str] = []
    for iface, addr in _distinct(pairs, desired):
        reason = _retire_owned_addr(iface, addr, run)
        if reason is not None:
            _log.warning("could not remove the panel-owned address %s", reason)
            keep.append((iface, addr))
            reasons.append(reason)
    return keep, reasons


def _rotation_refused(recorded: list[tuple[str, str]],
                      rotating: list[tuple[str, str]]) -> str:
    """The reason a rotation was declined at `STALE_LIMIT`, phrased for the operator.

    It travels the ordinary address-failure channel (`_provision_result` -> `ok=False` -> the
    caller's rollback and `/api/ready`'s `provisioning` check), because that is exactly what it is:
    an address the panel owns that it has not removed, and now also a change it has not applied.
    Both halves are named — what stayed put, and what has to happen before the change can land.
    """
    reason = (", ".join(f"{a} on {i}" for i, a in rotating)
              + f": left in place and the change refused before the kernel was touched — "
                f"{len(recorded)} panel-owned addresses are already awaiting removal, the most "
                f"this panel will track ({STALE_LIMIT}), and forgetting one to make room would "
                "strand it on the host; the segment keeps its current address until the backlog "
                "drains")
    _log.error("refusing to rotate the segment address: %s", reason)
    return reason


def reconcile_segment_addresses(store, plan: NetPlan, run=_run) -> list[str]:
    """Atomically replace the desired addresses, then delete only addresses the panel owns.

    Ownership is recorded BEFORE the kernel is touched, and an address that is being replaced
    stays recorded (on the stale list) until its `ip addr del` has actually run — and, now, until
    that removal is PROVEN: a refused or timed-out delete leaves the pair on the stale list and
    returns a reason, instead of clearing the record over an address that is still on the host.
    That ordering is what keeps the panel's record a superset of what it put on the host: a
    failure — or a caller whose surrounding DB transaction rolls back mid-apply — can then still
    find every address to remove, instead of leaving an orphan no later pass would ever delete.

    The ownership keys make config changes and IPv6 disablement safe on hosts which also carry
    unrelated addresses on the segment interface: no wildcard/flush operation is ever used.

    What the ledger may NOT contain is an address this pass is installing. A pair retained by a
    refused removal is still an address, and moving back to it (`A` -> `B` -> `A`) makes it the
    desired one again; the entry is dropped from the stale set here, before it is written, and the
    deletion site rejects it a second time (see `_desired_pairs`). Dropping the entry is not
    forgetting: the same address is being recorded as the segment's current one on the line below.

    A rotation that would push the ledger past `STALE_LIMIT` is REFUSED here instead, before the
    record is written and before the kernel is touched, and the refusal is returned as the reason.
    The interface keeps the address it already has, every ownership record stays exactly as it was,
    and the caller — whose surrounding transaction rolls back on a failed pass — is left describing
    the configuration the host actually has.

    Returns a reason per address whose removal is not proven, for the caller to report.
    """
    old_iface = store.get_setting("managed_segment_iface") or plan.segment_iface
    old4 = store.get_setting("managed_segment_addr4") or ""
    old6 = store.get_setting("managed_segment_addr6") or ""
    new_iface = plan.segment_iface
    new4 = f"{plan.segment_ip}/24"
    new6 = host_addr6(plan.segment_ip6) if plan.ipv6_enabled else None

    desired = _desired_pairs(new_iface, new4, new6 or "")
    recorded = _distinct(_parse_stale(store), desired)
    superseded = []
    if old4 and (old4 != new4 or old_iface != new_iface):
        superseded.append((old_iface, old4))
    if old6 and (old6 != new6 or old_iface != new_iface):
        superseded.append((old_iface, old6))
    rotating = _distinct(superseded, desired | frozenset(recorded))
    if rotating and len(recorded) + len(rotating) > STALE_LIMIT:
        return [_rotation_refused(recorded, rotating)]
    stale = recorded + rotating

    _record_ownership(store, new_iface, new4, new6 or "", stale)

    run(["ip", "addr", "replace", new4, "dev", new_iface])
    if new6:
        run(["ip", "-6", "addr", "replace", new6, "dev", new_iface])

    keep, reasons = _retire_owned(stale, run, desired)
    _record_ownership(store, new_iface, new4, new6 or "", keep)
    return reasons


def clear_managed_addresses(store, run=_run) -> list[str]:
    """Remove only addresses previously installed by this panel and clear ownership state.

    Returns a reason per address whose removal is not proven. Each of those stays on the stale
    ledger — the current-address keys are cleared, because the panel no longer claims them as the
    segment's, but the pair is still recorded as owed a removal, and this same path runs on every
    later pass while segment management is off, so it retries. Reporting matters more here than
    anywhere else: with management off, readiness skips the segment-address check entirely, so the
    pass result is the only place a leftover the panel owns can be seen.
    """
    iface = store.get_setting("managed_segment_iface") or ""
    keep, reasons = _retire_owned(
        _parse_stale(store) + [(iface, store.get_setting("managed_segment_addr4") or ""),
                               (iface, store.get_setting("managed_segment_addr6") or "")], run)
    _record_ownership(store, "", "", "", keep)
    return reasons


def _nm_reload(run, nm_active) -> None:
    """Reload NetworkManager live via nsenter into pid 1, but only when it is running.

    Both shell-outs are in the runner's slow class: NM's own D-Bus timeouts are ~30s, so the
    cap is there for a wedged nsenter/nmcli, not a slow one. A cap that does fire lands in the
    `CalledProcessError` branch below, i.e. the same best-effort skip as an NM that refuses.
    """
    nm_active = nm_active or (lambda: _nm_active(run))
    if nm_active():
        try:
            run(["nsenter", "-t", "1", "-m", "-n", "--", "nmcli", "general", "reload"])
        except (subprocess.CalledProcessError, OSError):
            pass


def ensure_nm_unmanaged(seg: str, run=_run, write_file=_write_file, nm_active=None) -> None:
    """Tell NetworkManager to leave the segment alone (so it doesn't fight our addressing).
    Writes the drop-in unconditionally (honored whenever NM (re)starts); reloads NM live via
    nsenter into pid 1 only when NM is actually running."""
    write_file(NM_CONF_PATH, f"[keyfile]\nunmanaged-devices=interface-name:{seg}\n")
    _nm_reload(run, nm_active)


def remove_nm_unmanaged(run=_run, remove_file=None, nm_active=None) -> None:
    """Hand the segment back to NetworkManager when the panel stops managing it.

    Without this the drop-in outlives the panel's ownership and NM refuses to manage the
    interface forever, so an operator who turns `manage_segment` off is left with a segment
    nobody configures.
    """
    (remove_file or _remove_file)(NM_CONF_PATH)
    _nm_reload(run, nm_active)


def ensure_segment_prefix6(store, settings, rand=secrets.token_bytes) -> str:
    """Resolve the segment v6 prefix for the current mode and return it:
    static CIDR -> unchanged; `auto` -> unchanged (the PD client owns it, Phase D);
    blank + v6 on -> generate a stable ULA, persist it, return it."""
    cur = (store.get_setting("segment_ip6") or settings.segment_ip6 or "").strip()
    v6_on = (store.get_setting("ipv6_enabled") or "0") == "1"
    if cur or not v6_on:
        return cur
    _, vid = parse_vlan(store.get_setting("segment_iface") or settings.segment_iface)
    ula = generate_ula_prefix(vid if vid is not None else 0, rand=rand)
    store.set_setting("segment_ip6", ula)
    _log.info("generated stable ULA prefix for the segment: %s", ula)
    return ula


def effective_segment_prefix6(store, settings, rand=secrets.token_bytes) -> str:
    """Return the /64 to install without mutating the configured ``auto`` intent.

    Auto mode prefers a currently delegated /64 and otherwise uses a persistent ULA fallback,
    so client IPv6 remains deterministic while the upstream PD lease is absent or renewing.
    """
    if (store.get_setting("ipv6_enabled") or "0") != "1":
        return ""
    intent = (store.get_setting("segment_ip6") or settings.segment_ip6 or "").strip()
    if intent.lower() != "auto":
        return ensure_segment_prefix6(store, settings, rand=rand)
    delegated = (store.get_setting("pd_segment_prefix6") or "").strip()
    if host_addr6(delegated):
        return delegated
    ula = (store.get_setting("ula_prefix6") or "").strip()
    if not host_addr6(ula):
        _, vid = parse_vlan(store.get_setting("segment_iface") or settings.segment_iface)
        ula = generate_ula_prefix(vid if vid is not None else 0, rand=rand)
        store.set_setting("ula_prefix6", ula)
        _log.info("generated stable ULA fallback for DHCPv6-PD: %s", ula)
    return ula


# --- candidate ledger (host state a rolled-back provisioning pass left behind) --------------

# `host_provision` runs INSIDE its caller's DB transaction and records what it installed through
# the same `set_setting` calls, so a later failure rolls that ownership metadata back while the
# address and the VLAN link it created stay on the host. The recovery pass and the readiness check
# then read the RESTORED metadata, which names the old interface — so when `segment_iface` changed,
# the orphan is invisible to the panel and sits outside the nft guard, which is scoped to that same
# old interface. The candidate is therefore recorded here, OUTSIDE the transaction, so the rollback
# — or the next boot, after a crash between the two — can still find it. Never returned by the API.
#
# ONE implementation, and deliberately down here rather than beside a route: `PUT /api/network` and
# `restore_backup` both change `segment_iface` through `host_provision`, and a recovery story that
# lived in the route was a recovery story the restore did not have.
#
# WHAT THE UNDO DOES WITH WHAT IT FINDS is split, and not symmetrically. A VLAN link is deleted,
# because the panel records ownership of one before it creates it and can prove afterwards that the
# link is gone. An ADDRESS is named and left: every record naming it was written before the kernel
# was touched, so it is intent, and each mechanism tried for turning that into proof had a path
# where a missing observation read as "the panel installed this" — which deletes the address the
# operator reaches the gateway on. Visibility was always where nearly all the value of this ledger
# was, and it cannot be falsified by a record that was lost.
PROVISION_UNDO_KEY = "pending_provision_undo"


@dataclass
class UndoOutcome:
    """What an undo did, and what it could not settle.

    `actions` is everything worth telling the operator (removals, failures, and the host state
    deliberately left in place); `unresolved` is the subset a later pass could still finish, which
    is what decides whether the pending record stays instead of being cleared. An address the undo
    reports is never unresolved: no pass will delete it, so keeping the record would repeat the
    same message forever with nothing to act on.
    """
    actions: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


def managed_host_state(store) -> dict:
    """The interface and addresses the panel currently claims ownership of."""
    return {"iface": store.get_setting("managed_segment_iface") or "",
            "addr4": store.get_setting("managed_segment_addr4") or "",
            "addr6": store.get_setting("managed_segment_addr6") or ""}


def candidate_link_probe(state, iface: str) -> tuple[str, str]:
    """The three-state host answer for `iface`, through the backend's own seam.

    Deliberately NOT `_link_exists`. That collapse — anything but a clean exit ⇒ not there — is
    right for the bring-up decision and is a licence to delete here: an unanswerable probe would
    read as "this link was not on the host before, so the panel must have created it", and the
    undo would remove an interface it does not own. On a live gateway that is the whole segment.
    """
    run = getattr(state.net, "_run", None)
    if run is None:
        return LINK_UNKNOWN, "the net backend has no host seam"
    if not iface:
        return LINK_UNKNOWN, "no interface"
    try:
        return _probe_link(iface, run)
    except (subprocess.CalledProcessError, OSError) as exc:
        return LINK_UNKNOWN, str(exc) or exc.__class__.__name__


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _projected_manage_segment(store, data: dict) -> bool:
    """Will host provisioning still be ON once this change lands?

    The PROJECTED value, not the stored one: a restore document imports `manage_segment` like any
    other allowlisted setting, so the pass that follows is gated on what the document says, not on
    what the store says while the candidate is being written. `PUT /api/network` cannot edit the
    key at all, so there the two are the same and the store answers. Blank or absent reads as on —
    `host_provision`'s own default.

    It matters because with segment management off that pass installs nothing; it CLEARS. A
    candidate recorded against it names host state the panel is not about to create, and undoing
    it later (at boot, say) removes state the following pass deliberately will not put back.
    """
    raw = (data["manage_segment"] if "manage_segment" in data
           else store.get_setting("manage_segment"))
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return True
    return _truthy(raw)


def provision_candidate(state, data: dict) -> dict:
    """What a host-provisioning pass for THESE incoming values may put on the host.

    `data` is the change about to be committed — the validated body of `PUT /api/network`, or the
    settings a restore document is about to import — so the candidate describes where the pass is
    headed, not where the store currently is. Empty when the projected `manage_segment` is off,
    because then the pass puts nothing anywhere.

    The link's prior state is recorded as one of the three probe answers, never as a bool and
    never by omission: only `LINK_ABSENT` proves the pass created the interface, and so only that
    answer may later license deleting it (see `undo_provision_candidate`). The probe is taken now,
    before the change, because now is the only moment the question can still be answered.

    The ADDRESSES carry no such state, deliberately. They were probed here too, when the undo
    still deleted them and needed a licence to; it no longer does — it names them and leaves them
    — so the probe answered a question nobody asks. Its absence cannot license anything.
    """
    if not hasattr(state.net, "_run"):      # linux-backend seam; the dry-run one touches no host
        return {}
    if not _projected_manage_segment(state.store, data):
        return {}
    plan = NetPlan.from_store(state.store, state.settings)
    iface = data.get("segment_iface") or plan.segment_iface
    ip = data.get("segment_ip") or plan.segment_ip
    ip6 = data.get("segment_ip6", plan.segment_ip6)
    ipv6_on = _truthy(data["ipv6_enabled"]) if "ipv6_enabled" in data else plan.ipv6_enabled
    link_state, why = candidate_link_probe(state, iface)
    if link_state == LINK_UNKNOWN:
        _log.warning("could not tell whether %s is already on the host (%s); a pass that has to "
                     "be rolled back will leave its link in place rather than delete one it "
                     "cannot prove it created", iface, why or "no answer from ip link show")
    addr4 = f"{ip}/24" if ip else ""
    # `auto`/blank resolve to a delegated or generated prefix inside the pass itself, so the
    # candidate v6 is knowable up front only for a static one. What the pass actually
    # claimed is read back straight afterwards and covers the rest for an in-process failure.
    addr6 = (host_addr6(ip6) or "") if ipv6_on else ""
    return {
        "iface": iface,
        "addr4": addr4,
        "addr6": addr6,
        "vlan": parse_vlan(iface)[1] is not None,
        "link_state": link_state,
    }


def record_provision_candidate(store, candidate: dict) -> None:
    """Persist the candidate so a rollback — or the next boot — can find it. Unguarded on
    purpose: a caller that cannot record what it is about to install has no recovery story, and
    must fail before it installs anything."""
    if candidate:
        store.set_setting(PROVISION_UNDO_KEY, json.dumps(candidate))


def clear_provision_candidate(store) -> None:
    """Drop the pending record. Guarded: nothing here may turn a finished operation into a
    failure, and a record that survives only costs one redundant undo attempt later."""
    try:
        store.set_setting(PROVISION_UNDO_KEY, "")
    except Exception:
        _log.warning("could not clear the pending host-provisioning undo", exc_info=True)


def _prior_link_state(candidate: dict) -> str:
    """The probe answer recorded when the candidate was written.

    A record from a release that stored a bool `link_existed` reads back as PRESENT or UNKNOWN,
    never ABSENT: that bool already folded "could not tell" into False, so treating False as
    proven absence would license deleting a link on the strength of a probe that never answered.
    One upgrade loses one cleanup opportunity; the other direction loses an interface.
    """
    state = str(candidate.get("link_state") or "")
    if state in (LINK_PRESENT, LINK_ABSENT, LINK_UNKNOWN):
        return state
    if "link_existed" in candidate:
        return LINK_PRESENT if candidate["link_existed"] else LINK_UNKNOWN
    return LINK_UNKNOWN


def _merge(into: UndoOutcome, other: UndoOutcome) -> UndoOutcome:
    into.actions.extend(other.actions)
    into.unresolved.extend(other.unresolved)
    return into


def _undo_candidate_link(state, candidate: dict, restored: dict, run) -> tuple[UndoOutcome, bool]:
    """Remove the VLAN link a rolled-back pass created, or say why it was left alone.

    Returns what to report and whether the link was actually DELETED. Only a delete settles the
    addresses as well — a link that goes takes every address on it with it — so only a delete
    lets the caller stop there. A link that is KEPT, for any of the reasons below, may still be
    carrying an address the pass installed on it, an address that is now outside the restored
    ownership ledger and invisible to every later pass; returning early on that would leave the
    operator with no mention of exactly the orphan this ledger exists to surface.

    Three separate things must be true before an `ip link delete` is issued: the candidate is a
    VLAN, the probe taken BEFORE the pass proved the link was not there (so the pass created it),
    and the link is not the one the restored state is using. A probe that cannot answer — then or
    now — is not one of them.
    """
    iface = candidate.get("iface") or ""
    if not (iface and candidate.get("vlan")) or iface == restored["iface"]:
        return UndoOutcome(), False
    prior = _prior_link_state(candidate)
    now, why = candidate_link_probe(state, iface)
    if now == LINK_ABSENT:              # already gone (reboot / a previous pass) — nothing to do
        return UndoOutcome(), False
    if prior != LINK_ABSENT:
        # Not ours to delete, and never will be: the prior state is recorded, so retrying cannot
        # turn it into proof. Report it once, loudly, and let the record be cleared — an orphan
        # the operator is told about beats an interface the panel removes on a guess.
        before = "never probed" if prior == LINK_UNKNOWN else prior
        reason = (f"the candidate link {iface} was left on the host: the panel cannot prove it "
                  f"created it (its state before the change was {before})")
        return UndoOutcome(actions=[reason]), False
    if now == LINK_UNKNOWN:
        reason = (f"could not tell whether the orphaned candidate link {iface} is still on the "
                  f"host ({why or 'no answer from ip link show'}); it was left in place")
        return UndoOutcome(actions=[reason], unresolved=[reason]), False
    try:
        run(["ip", "link", "delete", iface])
    except Exception as exc:
        reason = f"removing the orphaned candidate link {iface} failed: {exc}"
        return UndoOutcome(actions=[reason], unresolved=[reason]), False
    return UndoOutcome(actions=[f"removed the orphaned candidate link {iface}"]), True


# What the operator is told about the addresses a rolled-back pass may have left behind. One line
# per interface, naming every address on it, because the remedy is one visit to that interface.
_ORPHAN_ADDRS = (
    "addresses from the rolled-back change were left in place on {iface}: {addrs}. The panel "
    "deliberately does not delete addresses when a change is undone — it cannot prove which of "
    "them it installed, and one it did not install is the address this gateway is reached on. "
    "Check `ip addr show dev {iface}` and remove by hand any this change added.")


def _report_orphan_addrs(candidate: dict, installed: dict, restored: dict) -> UndoOutcome:
    """Name the addresses a rolled-back pass may have left on the host. DELETES NOTHING.

    This is the whole address half of the undo, and it is report-only by decision. Every address
    that reaches here was written into a record BEFORE the pass touched the host — the candidate
    ledger, or the ownership keys, which are equally written ahead of `ip addr replace` — so each
    is a statement of intent, not of ownership. Successive attempts to derive ownership anyway
    (a probe recorded in the candidate, then a second one journalled by the pass itself) each
    turned out to have a path on which losing or never taking an observation READ AS "the panel
    installed this", and the delete that followed is `ip addr del` against the address the
    operator reaches the gateway on. The value of this ledger was always the visibility; that part
    is safe, cannot be falsified by a lost record, and is all that is kept.

    Reported once, never `unresolved`: no later pass will delete these either, so keeping the
    pending record would only repeat the same message with nothing to act on. The link half still
    retries, because there a retry can still finish the job.

    Addresses the RESTORED ownership metadata still names are excluded — those are in use, not
    orphans. That also keeps the common case quiet: a change that never moved the segment leaves
    the candidate addresses equal to the restored ones, so there is nothing to report at all.
    """
    keep = {(restored["iface"], restored["addr4"]), (restored["iface"], restored["addr6"])}
    by_iface: dict[str, list[str]] = {}
    for iface, addr in ((installed.get("iface") or "", installed.get("addr4") or ""),
                        (installed.get("iface") or "", installed.get("addr6") or ""),
                        (candidate.get("iface") or "", candidate.get("addr4") or ""),
                        (candidate.get("iface") or "", candidate.get("addr6") or "")):
        if not (iface and addr) or (iface, addr) in keep:
            continue
        addrs = by_iface.setdefault(iface, [])
        if addr not in addrs:
            addrs.append(addr)
    return UndoOutcome(actions=[_ORPHAN_ADDRS.format(iface=iface, addrs=", ".join(addrs))
                                for iface, addrs in by_iface.items()])


def undo_provision_candidate(state, candidate: dict, installed: dict | None = None) -> UndoOutcome:
    """Remove host state a rolled-back provisioning pass left behind.

    Called from three places, and the point of hoisting it here is that they are the three the
    orphan can come from: the `PUT /api/network` rollback, `restore_backup` (whose restorable
    settings include `segment_iface`), and the boot path, for the crash that killed the first two
    before they got here.

    WHAT IT KEEPS is whatever the ownership metadata names, so the caller's placement decides what
    that is. The two in-process rollbacks run it AFTER their recovery pass, which has just
    rewritten those keys to name the state they went back to. The boot path runs it BEFORE the
    provisioning pass, where the keys are what the interrupted transaction rolled back to — the
    same answer, reached the other way — and where the pass that follows re-asserts the configured
    segment in full, so a record that reaches too far is repaired inside the same boot instead of
    outliving it. Anything the candidate installed that is not in that set is an orphan no later
    pass would look at. Needed even when the interface did not change — `ip addr replace` replaces
    one address and leaves any other in place — though there the leftover is at least visible to
    the readiness drift check, while one on a candidate interface is visible to nothing.

    `installed` is what the pass was read back as having claimed, known only in-process; the
    persisted record carries the candidate alone. It is the only place a v6 prefix the pass
    resolved for itself (`auto`/PD) ever appears, so it is what makes that address reportable.

    ONLY THE LINK IS EVER DELETED. Ownership of a link is established before the panel creates it
    and is provable afterwards, so removing one takes the addresses on it with it and is safe.
    Addresses on a link the panel does not own are a different question, and every mechanism for
    answering it had a path that read a missing observation as proof; they are therefore named and
    left in place (see `_report_orphan_addrs`). A record that carries the fields those mechanisms
    used — a pending undo written by an earlier release — is read for its interface and addresses
    like any other, and the extra fields are simply not consulted.
    """
    run = getattr(state.net, "_run", None)
    if run is None or not candidate:
        return UndoOutcome()
    restored = managed_host_state(state.store)
    outcome, link_deleted = _undo_candidate_link(state, candidate, restored, run)
    if link_deleted:
        # The link went and took every address on it with it; there is nothing left to report.
        return outcome
    return _merge(outcome, _report_orphan_addrs(candidate, installed or {}, restored))


def resume_pending_provision_undo(state) -> UndoOutcome:
    """Finish an interrupted candidate undo at boot. Never raises — nothing here may block a boot.

    This is what makes the record above worth writing: a crash between provisioning a candidate
    interface and undoing it used to strand host state that nothing would ever reclaim, because
    the only reader of the key was the rollback that had already died with it.

    IT RUNS BEFORE `host_provision`, AND THAT ORDER IS THE SAFETY. Whatever this removes, the
    pass that follows re-asserts from the stored configuration a moment later, so an undo that
    reaches too far is repaired inside the same boot. The reverse order has no such backstop: the
    undo would then be deleting state the pass had just created and recorded as owned — a link
    it had just added to its own ledger, an address it had just installed — leaving the ledger
    describing a host that no longer matches it and nothing to correct it until the next pass.

    A record it cannot use is discarded and reported rather than acted on: an unreadable value
    names no interface, so there is nothing it could safely remove and nothing a later boot could
    do better. A record whose work is not finished is KEPT, so the next pass retries it.
    """
    store = state.store
    try:
        raw = store.get_setting(PROVISION_UNDO_KEY) or ""
    except Exception:
        _log.warning("could not read the pending host-provisioning undo", exc_info=True)
        return UndoOutcome()
    if not raw.strip():
        return UndoOutcome()
    try:
        candidate = json.loads(raw)
        if not isinstance(candidate, dict) or not str(candidate.get("iface") or "").strip():
            raise ValueError("no candidate interface")
    except (ValueError, TypeError) as exc:
        _log.error("discarding an unusable pending host-provisioning undo (%s): %.200r", exc, raw)
        clear_provision_candidate(store)
        return UndoOutcome()
    _log.warning("a network change was interrupted before its host state could be reclaimed; "
                 "undoing candidate %s", candidate.get("iface"))
    try:
        outcome = undo_provision_candidate(state, candidate)
    except Exception as exc:
        # The record stays: this boot could not settle it, and a later pass may.
        _log.warning("the pending host-provisioning undo raised: %s", exc)
        return UndoOutcome(actions=[f"pending host-provisioning undo raised: {exc}"],
                           unresolved=[str(exc)])
    for line in outcome.actions:
        _log.warning("boot: %s", line)
    if outcome.unresolved:
        _log.error("host state from an interrupted network change is still unreclaimed and the "
                   "undo will be retried: %s", "; ".join(outcome.unresolved))
    else:
        clear_provision_candidate(store)
    return outcome


# --- orchestrator --------------------------------------------------------------

def _is_linux_backend(net) -> bool:
    """The real host backend carries the `_run` shell-out seam; DryRun (dev/CI) does not."""
    return hasattr(net, "_run")


def _set_result(state, result: NetResult) -> NetResult:
    state.provision_result = result
    return result


def _provision_result(links: list[str], addrs: list[str] = ()) -> NetResult:
    """The pass result, given the host state the panel OWNS and could not remove.

    A leftover the panel owns is not a debug detail: it is host state the pass intended to remove
    and did not, so the pass did not reach the state it is reporting. Both kinds travel the same
    `ok=False` channel every other provisioning failure uses — which is what makes the caller roll
    its candidate settings back, run the recovery pass, and name the problem to the operator
    instead of committing a config the host does not match.

    Addresses were added to it because the clear/reconcile paths used to swallow a refused or
    timed-out `ip addr del` entirely: the address stayed on the interface with its record dropped,
    and with segment management off — the one case where the clear path runs — readiness skips its
    address check, so nothing anywhere mentioned it. This result is now the thing that does, and
    it also fails `/api/ready`'s `provisioning` check, which reads the same object.
    """
    parts = []
    if links:
        parts.append("panel-created VLAN link not removed: " + "; ".join(links))
    if addrs:
        parts.append("panel-owned address not removed: " + "; ".join(addrs))
    if not parts:
        return NetResult(ok=True)
    return NetResult(ok=False, error="; ".join(parts))


def _pd_callback(state, run):
    """Build the callback used by PdClient's prefix-file watcher."""
    def changed(delegated: str | None) -> None:
        from pi_gw_panel.controller import apply_lock
        from pi_gw_panel.net_control.pd_client import derive_segment_prefix
        with apply_lock:
            store, settings = state.store, state.settings
            # Ignore a late hook notification after auto mode (or segment management as a
            # whole) has been disabled — otherwise this re-adds the addresses and restarts
            # the dnsmasq that the disable path just tore down.
            if ((store.get_setting("manage_segment") or "1") != "1"
                    or (store.get_setting("ipv6_enabled") or "0") != "1"
                    or (store.get_setting("segment_ip6") or "").strip().lower() != "auto"):
                return
            _, vid = parse_vlan(store.get_setting("segment_iface") or settings.segment_iface)
            selected = derive_segment_prefix(delegated, vid or 0) if delegated else None
            if delegated and selected is None:
                _log.warning("ignoring unusable delegated IPv6 prefix: %s", delegated)
                return
            try:
                store.set_setting("pd_segment_prefix6", selected or "")
                plan = NetPlan.from_store(store, settings)
                plan.segment_ip6 = effective_segment_prefix6(store, settings)
                addr_failures = reconcile_segment_addresses(store, plan, run=run)
                dnsmasq = getattr(state, "dnsmasq", None)
                if dnsmasq is not None and (store.get_setting("manage_dnsmasq") or "1") == "1":
                    dnsmasq.apply(render_dnsmasq(plan))
                # A superseded address this could not remove is reported here too: the watcher is
                # the one caller with no request to fail, so its result is the only surface.
                _set_result(state, _provision_result([], addr_failures))
            except Exception as exc:
                _set_result(state, NetResult(ok=False, error=f"PD prefix apply failed: {exc}"))
                raise
    return changed


def _stop_pd(pd) -> None:
    """Stop the PD client and discard its last delegation. Never raises."""
    try:
        pd.stop()
        clear_state = getattr(pd, "clear_state", None)
        if clear_state is not None:
            clear_state()
    except Exception as exc:
        _log.warning("stopping the DHCPv6-PD client failed: %s", exc)


def host_provision(state) -> NetResult:
    """Idempotent host gateway bring-up. Gated on the linux backend + `manage_segment`.
    Never raises out — a provisioning failure is logged, not fatal to boot. Re-entrant under
    the controller apply-lock so it can't interleave with a tunnel apply."""
    store, settings = state.store, state.settings
    if not _is_linux_backend(state.net):
        return _set_result(state, NetResult(ok=True))
    from pi_gw_panel.controller import apply_lock
    run = getattr(state.net, "_run", _run)
    pd = getattr(state, "pd_client", None)
    stop_pd = False
    with apply_lock:
        try:
            dnsmasq = getattr(state, "dnsmasq", None)
            if (store.get_setting("manage_segment") or "1") != "1":
                stop_pd = True
                store.set_setting("pd_segment_prefix6", "")
                if dnsmasq is not None:
                    dnsmasq.stop()
                addr_failures = clear_managed_addresses(store, run=run)
                link_failures = clear_managed_link(store, run=run)
                remove_nm_unmanaged(run=run)
                result = _set_result(state, _provision_result(link_failures, addr_failures))
            else:
                ensure_sysctls(settings)
                ensure_segment_prefix6(store, settings)
                plan = NetPlan.from_store(store, settings)
                plan.segment_ip6 = effective_segment_prefix6(store, settings)
                link_failures = ensure_segment_link(store, plan, run=run)
                addr_failures = reconcile_segment_addresses(store, plan, run=run)
                ensure_nm_unmanaged(plan.segment_iface, run=run)
                auto_pd = (plan.ipv6_enabled
                           and (store.get_setting("segment_ip6") or "").strip().lower() == "auto")
                if pd is not None:
                    if auto_pd:
                        set_callback = getattr(pd, "set_callback", None)
                        if set_callback is not None:
                            set_callback(_pd_callback(state, run))
                        pd.start()
                    else:
                        stop_pd = True
                        store.set_setting("pd_segment_prefix6", "")
                if dnsmasq is not None and (store.get_setting("manage_dnsmasq") or "1") == "1":
                    dnsmasq.apply(render_dnsmasq(plan))
                elif dnsmasq is not None:
                    dnsmasq.stop()
                # The rest of the pass still runs: the new segment must come up even when a
                # superseded link or address refuses to go, and the pass then reports that
                # leftover — a config the host does not match must not commit as a success.
                result = _set_result(state, _provision_result(link_failures, addr_failures))
        except Exception as exc:    # never crash boot on a provisioning hiccup
            _log.warning("host_provision failed: %s", exc)
            result = _set_result(state, NetResult(ok=False, error=str(exc)))
    # Outside the apply-lock on purpose: the PD watcher takes that same lock inside its
    # callback, so joining its thread while holding it would block for the whole join
    # timeout. The store state that makes a late callback a no-op is already committed above.
    if stop_pd and pd is not None:
        _stop_pd(pd)
    return result
