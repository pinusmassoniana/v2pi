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
from dataclasses import dataclass, field, replace

from pi_gw_panel.net_control.linux import TIMEOUT_RETURNCODE, _run, _write_proc
from pi_gw_panel.net_control.plan import NetPlan, NetResult
from pi_gw_panel.net_control.render import _IFACE_RE, render_dnsmasq

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


# --- durable records: the one place a settings write becomes a fact ---------------------------
#
# Everything this module remembers between passes lives in the settings k/v store, and every one of
# those records is load-bearing: the enforcement cover is derived from them, the links and addresses
# the panel owns are retired from them, and a record that stops describing the host is how an
# interface ends up live and outside the ruleset. The store offers no transactional guarantee and no
# typed answer — `set_setting` can raise, and it can also return having written NOTHING, while
# `get_setting` can raise or hand back something unparseable — so every call site used to implement
# its own verification, and each one got it wrong differently: a failed read read as "the record is
# empty", a write verified against the entries just requested instead of the whole record it
# replaced, a clear that took a non-raising write as proof of deletion. Those are one missing
# abstraction, not three bugs, so the store access lives here and the sites below use nothing else.
#
# THREE OPERATIONS, each named for the failure it exists to catch:
#
#   read  — a `Record`: the content, or UNKNOWN with the reason. Unknown is not empty, and the type
#           makes reading it as empty fail loudly instead of quietly (see `Record`).
#   write — writes, then PROVES it by reading back the COMPLETE expected content. A set record is
#           journalled first, so the replace can never be the step that loses an entry the record
#           already held (see `write_set`).
#   clear — proven the same way, and by writing a TERMINAL form its readers ignore rather than by
#           trusting the blanking, so a silent no-op cannot leave a live-looking record behind.
#
# NOTHING HERE RAISES on a store failure; each returns the reason instead, because every caller has
# something better to do with it than an exception — decline to touch the host before it can record
# what it is about to do, keep an interface covered, or report through the pass result. The one
# deliberate exception is on the READ side: asking an UNKNOWN record for its content raises
# `RecordUnknown`, because there is no answer to give and no safe default to invent.


class RecordUnknown(RuntimeError):
    """A record that could not be read, asked for its content anyway."""


@dataclass(frozen=True)
class Record:
    """What one durable record SAYS, which is two facts and cannot be one.

    `known` — the store answered. The content — `.text`, `.lines()`, `.pairs()` — is reachable only
    then, and asking for it otherwise raises.

    UNKNOWN IS NOT EMPTY. That collapse is the defect this type exists to end, and it had the same
    shape everywhere it appeared: a `try`/`except` around the read that returned `[]`, so "the store
    could not tell us which interfaces may still be carrying the segment" became "no interface is",
    and the enforcement narrowed off one that may have been live. So the absent content is `None`
    and never `""` — a caller that reaches past the guard gets an `AttributeError` on the next line
    rather than a plausible empty answer — and `__bool__` refuses to guess which of the two facts
    `if record:` meant, for the same reason `AddressOutcome` refuses to be a truth value.
    """
    raw: str | None
    reason: str = ""

    @property
    def known(self) -> bool:
        return self.raw is not None

    def __bool__(self):
        """Never a truth value: "the record is empty" and "there is no answer" are both falsey."""
        raise TypeError("a record read is two facts: read .known, then its content")

    def _content(self) -> str:
        if self.raw is None:
            raise RecordUnknown(self.reason or "the record could not be read")
        return self.raw

    @property
    def text(self) -> str:
        """The whole value, stripped. Raises when the record is unknown."""
        return self._content().strip()

    def lines(self) -> list[str]:
        """The record's entries, in order, without blanks or repeats. Raises when unknown."""
        out: list[str] = []
        for line in self._content().splitlines():
            entry = line.strip()
            if entry and entry not in out:
                out.append(entry)
        return out

    def names(self) -> list[str]:
        """The INTERFACE-NAME entries — ALL OF THEM OR NONE. Raises when unknown, and when any
        entry is not one legal interface token.

        The same all-or-nothing rule as `pairs()`, one token wide instead of two, and it exists for
        the same reason: a record that cannot be understood in full is UNKNOWN, not partly known.
        The single-token format used to be treated as self-validating — "whatever a mangled line
        says is carried as a name, which covers an interface that may not exist, free" — and that
        was wrong in the one direction that matters. `eth0.2 eth0.9`, the shape a hand-edited
        database or a truncated write produces, is not two names and it is not a name that
        over-covers: it becomes ONE bogus name, `known`, covering nothing that exists, while
        neither real interface is named in the drop or the redirect. It does not fail its own
        delete either — `ip link delete "eth0.2 eth0.9"` is answered "does not exist", which is the
        one answer that PROVES absence, so the entry is dropped from the ledger and the two live
        interfaces it stood for are forgotten.

        So the shape is checked, against the same expression the render interpolates through —
        `render._IFACE_RE`, IMPORTED and not restated, because a second copy of it here is how the
        ledger and the ruleset it feeds come to disagree about what a name is. Whitespace, quotes and
        anything past IFNAMSIZ cannot be an interface name, so an entry carrying them names nothing,
        and the safe answer is the one the read side already gives — say so, and let the reason
        travel to a caller that has to keep covering.
        """
        out: list[str] = []
        for entry in self.lines():
            if not _IFACE_RE.fullmatch(entry):
                raise RecordUnknown(
                    f"the record holds {entry[:80]!r}, which is not one interface name, so what it "
                    "names cannot be read in full")
            if entry not in out:
                out.append(entry)
        return out

    def pairs(self) -> list[tuple[str, str]]:
        """The `(iface, addr)` entries — ALL OF THEM OR NONE. Raises when unknown, and when any
        entry is not a pair.

        STRUCTURED PARSING IS ALL-OR-NOTHING, and a record that cannot be understood in full is
        UNKNOWN, not partly known — which is the same rule the read side follows, one level down.
        Malformed nonblank entries used to be skipped here while the record stayed `known`, so a
        truncated entry — `eth0.2` where `eth0.2 192.168.9.2/24` was written — produced
        `Cover(known=True, names=[])` with `may_narrow` true, and the pass narrowed the enforcement
        off the very interface that entry existed to keep named. "The store told us something we
        cannot read" is not a weaker problem than "the store could not tell us", and it has the same
        safe answer: say so, and let the reason travel to a caller that has to keep covering.
        """
        out: list[tuple[str, str]] = []
        for entry in self.lines():
            parts = entry.split()
            if len(parts) != 2:
                raise RecordUnknown(
                    f"the record holds {entry[:80]!r}, which is not an interface and an address, "
                    "so what it names cannot be read in full")
            if (parts[0], parts[1]) not in out:
                out.append((parts[0], parts[1]))
        return out


def _store_get(store, key: str) -> tuple[object, str]:
    """The raw value at `key`, or the reason THE STORE could not say. `(value, reason)`. Never raises.

    The one place a read calls `get_setting`, so the two ways a read fails stay TELLABLE APART above
    it: a non-empty `reason` is the store refusing to answer, and an empty one means the store DID
    answer — with `None`, its own "no such key", with text, or with a value of a type no record may
    hold. Which of those two a reader may act on differs (see `_read_scalar`), and a reader handed one
    collapsed answer cannot make the distinction at all.
    """
    try:
        return store.get_setting(key), ""
    except Exception as exc:            # any store fault: it is one answer, "we cannot tell"
        return None, (str(exc) or exc.__class__.__name__)


def _unreadable(key: str, reason: str) -> Record:
    """An UNKNOWN record, reported where it is read."""
    _log.error("the durable record %s could not be read: %s", key, reason)
    return Record(None, reason)


def read_record(store, key: str) -> Record:
    """What `key` holds, or UNKNOWN with the reason the store could not say. Never raises.

    An unset record is EMPTY (`None` is how this store says "no such key"), and anything that is not
    text is UNKNOWN rather than something coerced into text: `Record` promises its content is a string
    and every reader below builds on that, so a store handing back another type is a store that could
    not tell us what the record holds.

    THE TWO UNKNOWNS ARE ONE ANSWER HERE, DELIBERATELY, and this is the right reader for every record
    whose content the panel cannot write again from scratch — a LEDGER above all, whose entries may be
    the only record of something that is on the host. There the two cost the same: nothing may be
    narrowed, nothing deleted, and nothing healed either. A SCALAR the panel rewrites IN FULL on every
    pass is the case where the collapse costs a durable wedge, and it has its own reader
    (`_read_scalar`).
    """
    raw, why = _store_get(store, key)
    if why:
        return _unreadable(key, why)
    if raw is None:
        return Record("")
    if not isinstance(raw, str):
        return _unreadable(key, f"it holds {raw!r}, which is not text")
    return Record(raw)


def _read_scalar(store, key: str) -> tuple[Record, str]:
    """One scalar the panel writes IN FULL: `(record, invalid)` — the record, and the reason it holds
    something that is not a value at all. Never raises.

    THE DISTINCTION `read_record` COLLAPSES, and the reason this reader exists. "The store would not
    answer" and "the store answered with something that is not text" are two different facts about a
    read, and only the first justifies refusing: a store fault says nothing about the host, so nothing
    may be concluded; returned-invalid data is BAD DATA IN A RECORD THIS PASS REWRITES, so the pass
    that reads it is also the repair.

    Read through `read_record`, a non-text `managed_segment_iface` made the whole `Ownership`
    unreadable, `_ownership_cover` raise and `_provision_segment` decline before it configured
    anything — on that pass and on every later one, because nothing that far up the pass ever rewrites
    the scalar. That is the permanent provisioning wedge the invalid-versus-blank distinction was built
    to rule out (see `Ownership`), reached through the TYPE check rather than the shape check, and one
    hand-edited row or one foreign writer is all it takes.

    So the two answers separate here. A store fault is UNKNOWN with `invalid` empty, exactly as before.
    A value that is not text is UNKNOWN TOO — so no reader can take `.text` off it and get a plausible
    blank — and its reason travels back in `invalid`, where it authorises no comparison and no
    deletion, is reported to the operator, and is overwritten by the pass on its way past. It is
    deliberately NOT logged here: it is reported once, where the pass reports it
    (`_invalid_ownership_reasons`), and a boundary that is read five times a pass may not narrate the
    same value five times.
    """
    raw, why = _store_get(store, key)
    if why:
        return _unreadable(key, why), ""
    if raw is None:
        return Record(""), ""
    if not isinstance(raw, str):
        reason = (f"{key} holds {raw!r}, which is not text, so what the panel claims there cannot be "
                  "read as anything it wrote")
        return Record(None, reason), reason
    return Record(raw), ""


def _verified_write(store, key: str, text: str, expect: list[str] | None) -> str:
    """Write `text` to `key` and read it back. "" or why the record does not hold it.

    `expect` is the entry list the read-back must equal; `None` asks for the value verbatim. The
    read-back is the whole point: a `set_setting` that returns having written nothing is
    indistinguishable from one that worked until the record is read.
    """
    try:
        store.set_setting(key, text)
    except Exception as exc:            # any store fault: the record does not hold it either way
        return f"{key} could not be written: {exc or exc.__class__.__name__}"
    back = read_record(store, key)
    if not back.known:
        return f"{key} could not be read back after being written: {back.reason}"
    if expect is None:
        if back.text == text.strip():
            return ""
        return (f"{key} reads back as {back.text[:80]!r}, not the {text.strip()[:80]!r} that was "
                "written")
    held = back.lines()
    if held == expect:
        return ""
    missing = [entry for entry in expect if entry not in held]
    if missing:
        return f"{key} does not name {'; '.join(missing)} after being written"
    return f"{key} reads back as {held} after {expect} was written"


def write_record(store, key: str, text: str) -> str:
    """Write one scalar record and prove it landed. "" or why it did not. Never raises."""
    return _verified_write(store, key, text, None)


def _journal_key(key: str) -> str:
    """Where a set record's complete expected content goes before the record itself is replaced."""
    return f"{key}_journal"


def read_set(store, key: str) -> Record:
    """A set record: the ledger AND its write-ahead journal, read as one. Never raises.

    UNKNOWN when either half is, because an unreadable half is not an empty one. The journal is
    normally blank and is read anyway: what it holds is the complete expected content of a replace
    that was not proven, which is exactly the state in which the ledger alone is missing an entry.
    """
    held = read_record(store, key)
    if not held.known:
        return held
    journalled = read_record(store, _journal_key(key))
    if not journalled.known:
        return Record(None, journalled.reason)
    return Record("\n".join(held.lines() + journalled.lines()))


def write_set(store, key: str, entries) -> str:
    """Replace the set at `key` with `entries`, and prove the record holds every one of them.

    THE COMPLETE EXPECTED CONTENT, NEVER THE DELTA. Verifying only the entries a caller has just
    added passes a partial write that keeps the new entry and drops an older one — and for a ledger
    whose entries are the interfaces the ruleset must name, the dropped one is uncovered
    permanently, because nothing will ever ask for it again.

    The complete content is JOURNALLED FIRST, under its own key, and the journal is cleared only
    once the record is proven to hold everything. So the replace cannot be the step that loses an
    entry: while the record is unproven the journal still names the whole set and `read_set` reads
    the two as one, which keeps the entry COVERED rather than merely reporting that it went.

    The exact content is verified, so a partial write in either direction is reported. What is left
    behind on a failure is always the over-covering direction — an interface named in a rule it no
    longer needs costs nothing, and the drain removes it once the host proves it gone.
    """
    expect = list(dict.fromkeys(entry.strip() for entry in entries if entry and entry.strip()))
    text = "\n".join(expect)
    why = _verified_write(store, _journal_key(key), text, expect)
    if why:
        return why                          # the record is untouched, so nothing has been lost
    why = _verified_write(store, key, text, expect)
    if why:
        return why                          # the journal still names the whole set
    _blank(store, _journal_key(key))        # best effort: an unblanked journal only repeats it
    return ""


def _blank(store, key: str) -> None:
    """Best-effort blanking of a record whose meaning is already settled. Never raises."""
    try:
        store.set_setting(key, "")
    except Exception:                   # already settled; there is nothing this could cost
        _log.debug("could not blank %s; what it still holds is already settled", key)


def clear_record(store, key: str, terminal: str = "") -> str:
    """Take `key` to a state no reader treats as live, and PROVE it got there. Never raises.

    A non-raising write is not proof of deletion, and the blanking is the least trustworthy half of
    a clear: a store that quietly keeps nothing leaves the OLD value in place, and for a record
    whose presence means "this interface may still be live" that is the direction that costs
    something — the ruleset keeps naming the interface for ever, and can capture unrelated traffic
    once that name is reused.

    So for such a record the VERIFIED step is writing a TERMINAL form its readers deliberately
    ignore, and the blanking is best effort on top of it: whatever becomes of the blank write, what
    is left either reads as settled or is reported here. A record with no terminal form (a ledger,
    where empty already means nothing) is simply blanked and read back.
    """
    if not terminal:
        return _verified_write(store, key, "", None)
    why = write_record(store, key, terminal)
    if why:
        return why
    _blank(store, key)
    return ""


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


# Whether a link is UP is a fourth answer about it, and a separate question from whether it is
# there: a link can be present and down (a VLAN this panel just created), present and up (the
# operator's own interface), or absent, which carries nothing and so answers the up-question with
# `LINK_DOWN`. It is asked in one place — the PD watcher, deciding whether the segment is in a
# state where dnsmasq may be pointed at it — and there only `LINK_UP` is an answer to act on.
LINK_UP = "up"
LINK_DOWN = "down"


def _probe_iface_up(iface: str, run=_run) -> tuple[str, str]:
    """Ask the host whether `iface` is administratively UP. `(state, reason)`.

    `LINK_UP` requires the host to say so. An explicit not-found is `LINK_DOWN`: a device that is
    not there carries no traffic, which is the fact the callers want. Everything else —
    refused, a netlink error, the runner's time limit (whose synthetic stderr describes the
    command it killed and says nothing about the device), output that cannot be read, or output
    with no flags in it — is `LINK_UNKNOWN`, and no caller may read that as UP. The flag is
    matched as a whole token, so `LOWER_UP` on a link whose admin state is down is not an answer.
    """
    if not iface:
        return LINK_UNKNOWN, "no interface"
    try:
        out = _run_stdout(run(["ip", "-o", "link", "show", iface]))
    except subprocess.CalledProcessError as exc:
        err = ((exc.stderr or "") + " " + (exc.stdout or "")).lower()
        if exc.returncode != TIMEOUT_RETURNCODE and any(t in err for t in _LINK_ABSENT_TOKENS):
            return LINK_DOWN, ""
        reason = (exc.stderr or "").strip() or f"ip link show exited {exc.returncode}"
        return LINK_UNKNOWN, reason
    except OSError as exc:
        return LINK_UNKNOWN, str(exc) or exc.__class__.__name__
    if out is None:
        return LINK_UNKNOWN, "ip link show produced no readable output"
    start, end = out.find("<"), out.find(">")
    if start < 0 or end < start:
        return LINK_UNKNOWN, "ip link show did not report the link flags"
    return (LINK_UP if "UP" in out[start + 1:end].split(",") else LINK_DOWN), ""


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


def _addr_token(token: str):
    """`token` as the ADDRESS AND PREFIX LENGTH it spells, or None when it spells no such thing.

    THE ONE PLACE A TOKEN BECOMES AN ADDRESS, and it is a comparison primitive, not a validator: it
    answers questions about two strings by asking them of the two addresses instead. `ip addr show`
    prints the kernel's own canonical spelling, a record may hold any spelling of the same address,
    and comparing the two as TEXT reads a live address as absent — which for the survivor ledger is
    the one answer that takes an interface out of the enforcement cover (see `drain_enforcement_cover`).

    A missing prefix is no answer at all rather than a `/32`: the whole-token contract below is what
    keeps a `192.168.10.2/24` from being answered for by a `192.168.10.2/16` some other owner put
    there, and a bare address would silently mean `/32` and match neither. A SCOPE is refused for the
    other direction — `fe80::1%eth0` and `fe80::1%eth1` are different addresses on the host and
    `ipaddress` drops the scope from the canonical spelling, so a scoped token compared canonically
    would answer about whichever interface asked.
    """
    if "/" not in token or "%" in token:
        return None
    try:
        return ipaddress.ip_interface(token)
    except ValueError:
        return None


def _probe_addr(iface: str, addr: str, run=_run) -> tuple[str, str]:
    """Ask the host whether `addr` is on `iface`. `(state, reason)`; `reason` explains an unknown.

    The CIDR is matched as a whole token, exactly as `ip addr del` matches it — address AND
    prefix length — so a `192.168.10.2/24` whose record this would drop is never answered for by
    a `192.168.10.2/16` some other owner put there.

    MATCHED AS AN ADDRESS AND NOT AS TEXT, though, and that is what this comparison had wrong. The
    kernel prints one spelling; a record can hold any of the spellings that mean the same address —
    `192.168.10.2/255.255.255.0`, an expanded or upper-case IPv6 — and `_checked_addr_text` accepted
    every one of them while handing the ORIGINAL token back. String equality against `ip addr show`
    then answered `ADDR_ABSENT` for an address that is on the interface: the survivor drained, the
    interface left the enforcement cover, and a real segment address was still on it. So both sides
    are parsed (see `_addr_token`) and the answer is about the addresses. A token this cannot read at
    all is `ADDR_UNKNOWN` — never absence, for the reason below.

    A device that is not on the host carries no addresses, so an explicit not-found answers
    `ADDR_ABSENT`. Everything else — refused, a netlink error, the runner's time limit (whose
    synthetic stderr describes the command it killed and says nothing about the interface), or a
    command that exited cleanly and produced no readable output at all — is `ADDR_UNKNOWN`.
    Never absence: absence is the one answer that lets an ownership record be forgotten.

    THE INTERFACE IS NOT CHECKED FOR ITS SHAPE HERE, and it no longer has to be: every caller proves
    its own entries before it asks — `_checked_survivors` for the survivor ledger, `_read_ownership`
    for the four ownership records — so a name that is not a name never reaches this at all. Which is
    the only place it can be stopped: a device the host does not have is an explicit not-found, and
    this reads that as `ADDR_ABSENT` — the one answer that lets an ownership record be forgotten. So
    the shape is checked where the record is READ, while the value can still be reported instead of
    spent as absence.
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
    want = _addr_token(addr)
    if want is None:                    # not an address and a prefix: the probe cannot ask about it
        return ADDR_UNKNOWN, f"{addr[:80]!r} is not one address with a prefix length"
    return (ADDR_PRESENT if any(_addr_token(tok) == want for tok in out.split())
            else ADDR_ABSENT), ""


def _nm_active(run=_run) -> bool:
    """True if a NetworkManager is running on the host (so a reload is meaningful)."""
    try:
        run(["nsenter", "-t", "1", "-m", "-n", "--",
             "systemctl", "is-active", "--quiet", "NetworkManager"])
        return True
    except (subprocess.CalledProcessError, OSError):
        return False


def ensure_sysctls(settings, write_proc=None) -> None:
    """Forwarding (v4 + v6) and accept_ra=2 on the uplink (so the Pi keeps its own v6 default
    route even with forwarding on). Best-effort; the privileged container has writable
    /proc/sys.

    THIS IS THE STEP THAT OPENS THE FORWARD PATH, which is why boot resolves the leak-guard and its
    fallback before the pass that calls it (see `app.create_app`) and why the writer is resolved at
    CALL time, exactly as `_disable_forwarding` resolves it: bound as a default, it is the module's
    real `/proc` writer, so a suite running as root would turn the host's own forwarding ON while
    testing that boot does not.
    """
    write = write_proc or _write_proc
    write("/proc/sys/net/ipv4/ip_forward", "1")
    write("/proc/sys/net/ipv6/conf/all/forwarding", "1")
    write(f"/proc/sys/net/ipv6/conf/{settings.mgmt_iface}/accept_ra", "2")


LINK_KEY = "managed_segment_link"


def _parse_links(store) -> list[str]:
    """Interface names recorded as VLAN links this panel created, oldest first.

    Newline-joined, the same encoding the stale-address ledger uses. A gateway upgrading from
    a release that stored a single bare name here reads back as a one-entry list, so the link
    it already owns stays recognised and cleanable instead of being orphaned by the format.

    RAISES `RecordUnknown` when the store cannot answer, and every caller wants that rather than an
    empty list: one decides whether to CREATE a link (and may not create one it cannot record), and
    the rest decide what the enforcement covers, where empty means "narrow".

    IT RAISES ON A MANGLED ENTRY TOO, for the whole record. The single-token format is not
    self-validating: an entry that is not one legal interface name is not an interface this
    over-covers, it is a name that covers nothing while the interfaces it stood for go unnamed —
    see `Record.names`, which is where that rule lives and why.
    """
    return read_set(store, LINK_KEY).names()


def _read_links(store) -> tuple[list[str] | None, str]:
    """The link ledger, or `(None, reason)` when it cannot be read IN FULL. Never raises.

    The `_read_pairs` of this ledger, and for the same callers: the two retirement paths, which may
    not raise and whose whole job is deleting what the ledger names. An unreadable record and one
    holding an entry that is not an interface name are ONE answer to both — the set of links they
    would act on is a set with something already dropped out of it — so neither touches the host and
    both report, with the entry quoted, because a malformed ledger is fixed on the host and not by
    guessing at it (the same stance as `_read_ownership`).
    """
    record = read_set(store, LINK_KEY)
    if not record.known:
        return None, record.reason
    try:
        return record.names(), ""
    except RecordUnknown as exc:
        return None, str(exc)


def _record_links(store, links: list[str]) -> str:
    """Replace the ownership ledger, proven. "" or why the record does not name every link.

    Unverified, this was the quietest of the module's write defects: `ensure_segment_link` records a
    VLAN before it creates it precisely so a kill between the two still leaves the panel owning it,
    and a `set_setting` that returned having kept nothing turned that into a link on the host that
    NOTHING owns — invisible to `retire_superseded_links` and to `clear_managed_link`, so no pass
    would ever delete it, and outside the drop-in and the ruleset the moment the segment moves on.
    """
    return write_set(store, LINK_KEY, links)


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
    # The forgetting is proven too. A write that kept nothing leaves the ledger naming a link that
    # is gone, which only costs a retried delete and one interface over-covered — the harmless
    # direction — but it is still a record that does not describe the host, so it is reported.
    why = _record_links(store, owned)
    if why:
        return (f"{link}: it is gone from the host, but the ownership ledger still names it, so a "
                f"later pass will try again: {why}")
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


def ensure_segment_link(store, plan: NetPlan, run=_run, link_exists=None) -> None:
    """Create the configured VLAN when needed. BRINGS NOTHING UP, AND RETIRES NOTHING.

    A VLAN the panel creates is appended to the ownership ledger BEFORE the kernel call, so
    disabling `manage_segment` can delete exactly the links this panel added (and never a
    pre-existing one) even if the process dies between the record and the creation. The ledger
    is a list for the same reason the stale-address one is: retargeting the segment
    (`eth0.2` -> `eth0.9`) must not overwrite the record of the link still on the host, or
    nothing would ever delete it.

    This is the CREATE step alone, and the other two steps of a retarget are elsewhere for two
    different reasons. The addresses the plan names need an interface to land on, so the creation
    has to go first — but nothing about the OLD link has to go before that, and retiring it here
    is what turned a rejected `ip addr replace` into a segment with no network at all (see
    `retire_superseded_links`); and nothing about the NEW link has to be LIVE before that either,
    which is why the bring-up left too (see `activate_segment_link`).

    The bring-up mattered for a different reason than the retirement, and a worse one. Every rule
    that constrains the segment names an interface — the kill-switch drop, the tproxy redirect,
    dnsmasq's listen address and DHCP range, the NetworkManager drop-in — and until this pass
    finishes they all still name the interface being replaced. Raising the candidate here made a
    half-applied pass — IPv4 installed and IPv6 refused, or the process killed between the two —
    leave an interface that is up and addressed and outside every one of them: a path around the
    policy, not merely an apply that failed, and one that persists for as long as the failure does.

    So a link this creates is created DOWN and stays down until every requested replacement has
    landed. There is deliberately no `ip link set … down` anywhere on the failure path to match:
    a candidate the panel did NOT create may already be up and carrying the operator's traffic,
    and taking an interface down is not something a pass may do on the strength of a plan it could
    not apply. Down-on-failure is a property of never having raised it, which also makes it hold
    when no failure path runs at all — a process killed between the two replacements leaves the
    candidate down because nothing ever brought it up, not because something caught the fall.
    """
    link_exists = link_exists or (lambda i: _link_exists(i, run))
    seg = plan.segment_iface
    parent, vid = parse_vlan(seg)
    owned = _parse_links(store)          # raises when the ledger cannot be read: see `_parse_links`
    if vid is not None and not link_exists(seg):
        if seg not in owned:
            owned.append(seg)
            # AND THE RECORD HAS TO BE PROVEN BEFORE THE KERNEL CALL, not merely attempted. The
            # whole point of writing it first is that a kill between the two lines still leaves the
            # panel owning the link — which a `set_setting` that quietly kept nothing does not give
            # you: the pass would then create a VLAN no ledger names, so nothing would ever retire
            # it and it would sit outside the drop-in and the ruleset the moment the segment moved
            # on. A pass that cannot record what it is about to install must install nothing.
            why = _record_links(store, owned)
            if why:
                raise RuntimeError(f"the panel could not record that it is about to create {seg}, "
                                   f"so it did not create it: {why}")
        run(["ip", "link", "add", "link", parent, "name", seg,
             "type", "vlan", "id", str(vid)])


def activate_segment_link(plan: NetPlan, run=_run) -> None:
    """Bring the segment link UP. THE COMMIT POINT of a segment move, and it has two preconditions.

    The plan's addresses must be on it, and the enforcement must already cover it — see
    `_provision_segment`, which is the only caller, and which stages the second before it performs
    the first. An interface that is up and addressed is reachable, and everything that CONSTRAINS
    the segment is scoped by interface name, so an interface raised ahead of either precondition
    is not a cosmetic ordering — it is a live interface the panel's own policy does not cover.

    It is `ip link set … up` and nothing else: idempotent, so re-running it on the segment that is
    already up (every ordinary pass) is a no-op, and safe to leave on the success path only. This
    module never brings a link DOWN — see `ensure_segment_link` for why the failure path has no
    counterpart to this one.
    """
    run(["ip", "link", "set", plan.segment_iface, "up"])


# --- the enforcement half of a segment move --------------------------------------------------
#
# Everything that CONSTRAINS the segment is scoped by interface name — the kill-switch drop, the
# tproxy redirect, the NetworkManager drop-in, dnsmasq's listen address — and the store names
# exactly one segment interface. So the ruleset the store renders cannot describe a host in the
# middle of a MOVE from one interface to another: for the length of that move both are live, the
# old one until it is retired and the new one from the moment it is raised, and a ruleset naming
# either alone leaves the other outside the drop and the redirect. That is not an apply that
# failed — it is a path around the panel's own policy, and it lasts as long as the move does.
#
# The pass performing the move applies a TRANSITIONAL ruleset naming every interface the segment
# may be reachable on before it raises anything, and narrows back to the configured interface alone
# once the superseded link is gone. A transitional ruleset is a superset, so every intermediate
# state it leaves behind — including one a kill interrupts — is at worst over-covered, never
# under-covered.
#
# The pass is not the only thing that has to know both names, though, and that is what the DURABLE
# COVER below is for. A move that fails, and a change that is rolled back, both END with a second
# interface up and carrying a segment address; the ruleset the store renders on the next pass, the
# next `sync_net` or the next boot would then narrow onto one of them while the other is still
# live. So what may be up is written down (`enforcement_cover`), every store-derived render
# consumes it, and an interface leaves it only when the host says it is gone.


def _other_ifaces(names, exclude: str) -> list[str]:
    """`names` in order, without blanks, repeats, or `exclude` (the plan's own interface)."""
    out: list[str] = []
    for name in names:
        if name and name != exclude and name not in out:
            out.append(name)
    return out


@dataclass
class Cover:
    """Interfaces the enforcement must name besides the plan's own, and whether that is the WHOLE
    answer.

    Two facts, for the same reason `AddressOutcome` is two. `names` is what the panel can name right
    now; `unknown` is why the answer may be short. A RENDER can only install the names it has, so it
    installs them — but the decision to NARROW is a different one, and may be taken only on a
    complete answer. An empty `names` alongside a reason in `unknown` means "nothing that could be
    read", never "nothing to cover", and those two used to be the same value: a store read that
    failed produced `[]`, the pass concluded its cover had emptied, and the ruleset narrowed off an
    interface that may have been up carrying the segment. Covering an interface that is already gone
    is free; uncovering one that may be live is the bypass this whole protocol exists to prevent.

    `__bool__` raises so `if cover:` cannot pick one of the two facts by accident.
    """
    names: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    @property
    def known(self) -> bool:
        """Every source answered, so `names` is the whole set."""
        return not self.unknown

    @property
    def may_narrow(self) -> bool:
        """Only a COMPLETE answer with nothing in it licenses narrowing the enforcement."""
        return not self.unknown and not self.names

    def __bool__(self):
        """Never a truth value: "nothing to cover" and "no answer" are both falsey and differ."""
        raise TypeError("a cover is two facts: read .names and .known (or .may_narrow)")

    def why(self) -> str:
        return "; ".join(self.unknown)


def _checked_names(values) -> list[str]:
    """`values` as interface names, raising `RecordUnknown` if any of them is not one.

    THE LAST GATE BEFORE A NAME BECOMES COVERAGE, and it is here rather than only in each source
    because every source feeds the same decision. A value that cannot be an interface name covers
    no interface: it is not a rule that matches nothing extra, it is a rule that matches nothing at
    all, standing in for whatever the entry was meant to say. Blank is not that — an unrecorded
    scalar is a legitimate "there is no such interface" and is simply skipped, exactly as
    `_other_ifaces` has always skipped it.
    """
    out: list[str] = []
    for value in values:
        if not isinstance(value, str):
            # NOT `str(value)`, and that is the whole point of this line: coercion is what makes a
            # value of the wrong type look like a name. `str(0.9)` is `"0.9"`, which matches the
            # expression below, parses as VLAN 9, and reaches `ip link delete 0.9`.
            raise RecordUnknown(
                f"the panel's records name {value!r} as an interface that may be carrying the "
                "segment, which is not text, so what they name cannot be read in full")
        name = value.strip()
        if not name:
            continue
        if not _IFACE_RE.fullmatch(name):
            raise RecordUnknown(
                f"the panel's records name {name[:80]!r} as an interface that may be carrying the "
                "segment, which is not an interface name, so what they name cannot be read in full")
        out.append(name)
    return out


# --- the SHAPE of a durable record, checked once, where the record is read ----------------------
#
# Every field below arrives out of the store: JSON out of the pending undo, a text entry out of a
# ledger. Four rounds of hardening the readers have each ended in the same place — a value of the
# wrong TYPE was made to look like a value of the right one — and the last round's own fix is the
# clearest case of it. It resolved a type confusion by COERCING the value to text, and coercion
# makes malformed data look valid: `str(0.9)` is `"0.9"`, an interface name that passes
# `_checked_names`, parses as VLAN 9 and reaches `ip link delete 0.9`; `str([1])` is `"[1]"`, an
# address that goes into the survivor ledger as a survivor, is proven absent by the first probe, and
# takes the interface out of the enforcement cover while a real segment address may still be on it.
#
# So NOTHING here coerces. Each field is checked for the TYPE and the FORM the panel writes there,
# at the one boundary where the record is read, and a field that is neither makes the whole record
# unreadable — which everywhere below means unknown: covered, refusing, retained, and never a licence
# to act. Fields the panel does NOT write are not consulted at all, because a pending record written
# by an earlier release carries some and reading it has to keep working across the upgrade.


def _checked_text(record: dict, field: str) -> str:
    """`field` as the TEXT the panel wrote there, stripped. An absent field reads as "".

    Raises `RecordUnknown` for anything that is not a string — a JSON number, a list, an object,
    `null`, a bool. Each of those is a record no writer here produces, and the one thing that may
    never be done with it is to turn it into the string it is not.
    """
    if field not in record:
        return ""
    value = record[field]
    if not isinstance(value, str):
        raise RecordUnknown(f"{field} is recorded as {value!r}, which is not text, so the record "
                            "cannot be read as one the panel wrote")
    return value.strip()


def _checked_addr_text(addr: str, where: str) -> str:
    """`addr` as THE ONE CANONICAL SPELLING of an address with a prefix length, or `RecordUnknown`
    saying it is not one.

    ONE TOKEN AND A PREFIX, which is exactly what every writer here records (`f"{ip}/24"`,
    `host_addr6`) and exactly what the drain compares: `_probe_addr` matches the whole CIDR token
    against `ip addr show`, so a value the host can never print — `[1]`, a bare address whose prefix
    is missing, two tokens with a space between them — is a pair the FIRST probe reports as absent.
    That is the shape that took an interface out of the cover while the real segment address was
    still on it, so it is refused here instead: nothing is written and nothing is drained.

    AND ONE SPELLING, which is the half that was missing. The check accepted every spelling
    `ipaddress` accepts — `192.168.10.2/255.255.255.0`, an expanded or upper-case IPv6 — and then
    returned the ORIGINAL token, which was recorded and later compared, as text, against the one
    spelling the kernel prints. So a validated record could name an address that IS on the interface
    and be answered "absent" by the very first probe: the survivor drained, the interface left the
    enforcement cover, and the segment address was still on it. Exactly the defect the type check was
    added to prevent, reached through the value the check handed back.

    So what comes back is `parsed.with_prefixlen` — the spelling `ip addr show` prints — and it is
    what every caller records and compares. A SCOPED address is the one spelling that may not be
    canonicalised: `with_prefixlen` drops the scope, and `fe80::1%eth0` and `fe80::1%eth1` are
    different addresses on the host, so it is refused rather than silently turned into a question
    about whichever interface is asked. The panel writes no scoped address anywhere.
    """
    if addr.split() != [addr] or "/" not in addr:
        raise RecordUnknown(f"{where} is recorded as {addr[:80]!r}, which is not one address with a "
                            "prefix length")
    if "%" in addr:
        raise RecordUnknown(f"{where} is recorded as {addr[:80]!r}, which names a scope, so which "
                            "interface it is an address on cannot be read from the address alone")
    try:
        parsed = ipaddress.ip_interface(addr)
    except ValueError as exc:
        raise RecordUnknown(f"{where} is recorded as {addr[:80]!r}, which is not an address "
                            f"({exc})") from exc
    return parsed.with_prefixlen


def _checked_addr(record: dict, field: str) -> str:
    """`field` as an address, or "" when the record claims none there. Raises otherwise."""
    addr = _checked_text(record, field)
    return _checked_addr_text(addr, field) if addr else ""


def _checked_family_addr(addr: str, family: int, where: str) -> str:
    """`addr` in the one canonical spelling AND of the family the record it came out of NAMES.

    The canonical half is `_checked_addr_text`; this adds the half a key like
    `managed_segment_addr6` asserts by existing. A record is read for exactly one comparison — is
    the address the panel owns the one the plan wants — and that comparison is answered against the
    plan's value FOR THAT FAMILY. So a v4 value recorded under the v6 key is never equal to the v6
    address the plan names, which made it "superseded": the pass appended it to the stale ledger and
    `ip addr del`'d it off the segment interface. On a live gateway the value under the v6 key that
    is a v4 address is the v4 address the operator reaches the gateway on.

    Neither direction is a shape the panel writes — the two keys are written from `f"{ip}/24"` and
    `host_addr6` — so a crossed pair is a hand-edited database or a write that landed on the wrong
    key, and the one thing that may not be done with it is to compare it with an address of the
    other family and spend the inequality as a licence to delete.
    """
    canonical = _checked_addr_text(addr, where)
    version = ipaddress.ip_interface(canonical).version
    if version != family:
        raise RecordUnknown(
            f"{where} is recorded as {canonical[:80]!r}, which is an IPv{version} address under the "
            f"record that names the panel's IPv{family} one, so what it claims cannot be read")
    return canonical


def _canonical_addr(addr: str) -> str:
    """`addr` in the one canonical spelling, or unchanged when it is not an address at all.

    FOR THE PLAN'S SIDE OF EVERY COMPARISON. The record's side is canonicalised where the record is
    read (`_read_ownership`), and one canonicalised side is worse than neither: the whole point is
    that both sides are the same spelling of the same address. The plan's values already are —
    `f"{ip}/24"` and `host_addr6` both produce it — so this normally changes nothing and exists so
    that it cannot come to matter which of the two writers produced the value being compared.

    A value this cannot parse is handed back untouched rather than refused: it is the plan's, not a
    record's, and `ip addr replace` is about to reject it and say so with the kernel's own words.
    """
    token = _addr_token(addr)
    return addr if token is None else token.with_prefixlen


def _checked_flag(record: dict, field: str) -> bool | None:
    """`field` as the BOOL the panel wrote there, or `None` when the record makes no claim.

    A real bool or `null`, and nothing else. `"false"` is a non-empty string, so truthiness read it
    as a yes — and the two fields this guards, `vlan` and `link_existed`, are the two halves of the
    authorisation to delete a link (see `_vlan_claim`, `_checked_prior_link_state`).
    """
    if field not in record:
        return None
    value = record[field]
    if value is None or isinstance(value, bool):
        return value
    raise RecordUnknown(f"{field} is recorded as {value!r}, which is not an answer either way")


def _checked_link_state(record: dict) -> str:
    """`link_state` as one of the three answers a probe gives, or "" when it carries none."""
    state = _checked_text(record, "link_state")
    if state and state not in (LINK_PRESENT, LINK_ABSENT, LINK_UNKNOWN):
        raise RecordUnknown(f"link_state is recorded as {state[:80]!r}, which is not one of the "
                            "three answers a probe gives")
    return state


def _checked_candidate_iface(record: dict) -> str:
    """The interface a candidate record names, or "" when it names none.

    Raises `RecordUnknown` when the value is not text, or is text that is not one interface name —
    the same gate every cover source goes through, reached before any normalisation of the value.
    """
    iface = _checked_text(record, "iface")
    return _checked_names([iface])[0] if iface else ""


# --- the pending provisioning-undo record, as a TYPE ------------------------------------------
#
# Five rounds hardened the readers of this one record FIELD BY FIELD, and the class outlived all five,
# because the defects left are not about a field: they are about the record. A JSON object with a
# repeated key had already lost one of the two values before any field check ran. `link_state` and
# `link_existed` each passed on their own while contradicting each other, and the reader preferred the
# one that authorises a delete. `resolved` was reserved for the clear and consulted by nobody
# validating a live candidate. A blank `iface` was spent as "there is no candidate" before the rest of
# the record was looked at, so a record that does not conform was discarded as known-empty.
#
# None of those can be expressed as a check on one field, so the record stops being a dict at the
# boundary. It is DECODED once (duplicate-aware), CANONICALISED once (addresses in the one spelling
# the kernel prints), checked as A WHOLE, and handed to its readers as a `Candidate` — which has no
# fields but the five the panel means, and no way to reach a raw one. Everything below reads that
# object; nothing below reads the dict.


@dataclass(frozen=True)
class Candidate:
    """WHAT A PENDING PROVISIONING-UNDO RECORD SAYS, decoded once and true of the record as a whole.

    Five values, and they are the only five: the interface a pass may have put the segment on, the
    two addresses it was headed for, whether the panel created that interface as a VLAN, and the
    prior-link observation — which is one answer, whichever of the two spellings the record used
    (`link_state`, or the superseded `link_existed` bool it folds through `LINK_PRESENT`/
    `LINK_UNKNOWN` and never `LINK_ABSENT`).

    IT IS A TYPE AND NOT A DICT BECAUSE THE DICT IS WHAT KEPT GOING WRONG. Every defect in this
    record's history is a reader reaching a raw field and deriving a poorer answer from it:
    `candidate.get("vlan")` over JSON made `"false"` an authorisation to delete an interface,
    `str(candidate.get("iface") or "")` turned the number `0.9` into a name `ip link delete` accepted,
    `candidate.get("resolved")` made a record still naming a live interface read as settled. So there
    is nothing here to reach: `vlan` is a real bool because only a real bool could construct it,
    `iface` is one interface name or "", the addresses are canonical or the record did not decode, and
    `__getitem__`/`get` refuse rather than let a future reader re-derive any of it. `__bool__` refuses
    for the reason `Record`'s and `Cover`'s do — "there is no candidate" is `None`, not a falsey
    record, and the two must not be one value.
    """
    iface: str
    addr4: str
    addr6: str
    vlan: bool
    prior: str

    def __bool__(self):
        """Never a truth value: "there is no candidate" is `None`, and this is a record that exists."""
        raise TypeError("a candidate either exists or is None; ask .names_candidate for what it says")

    def __getitem__(self, field):
        raise TypeError(f"a candidate is a decoded record, not the dict it came from; {field!r} is "
                        "not reachable and re-deriving it from the raw record is the defect")

    def get(self, *_args, **_kwargs):
        raise TypeError("a candidate is a decoded record, not the dict it came from; read its fields")

    @property
    def names_candidate(self) -> bool:
        """Does this record name an interface a pass may have put the segment on?

        FALSE ONLY IN AN OTHERWISE CONFORMING RECORD, which is the whole reason it is a property of
        the decoded object and not a test on the raw one. A blank or absent `iface` is a legitimate
        "this change puts nothing anywhere" — what a pass that provisions nothing records — and the
        reader that discards such a record on that basis (see `_pending_candidate`) may only do so
        once everything else about the record has been proven. Asked of the raw dict, the discard ran
        FIRST: `{"iface": "", "addr4": [1]}` was cleared as known-empty while not conforming at all.
        """
        return bool(self.iface)


def _decoded_record(raw: str) -> dict:
    """`raw` as the JSON object it holds, with NOTHING LOST ON THE WAY IN. Raises.

    `RecordUnknown` for an object with a REPEATED KEY, `ValueError`/`TypeError` for text that is not
    JSON at all — two different answers, because the second names no interface and never did while
    the first may well be naming one that is up (see `_pending_candidate` for what each costs).

    THE DEFAULT DECODER SILENTLY KEEPS THE LAST VALUE, and that is a richer state flattened into a
    poorer one before a single field check can see it. `{"iface": "eth0.9", "iface": "eth0.2"}` is not
    a record about eth0.2: it is a record about two interfaces, one of which the enforcement would
    then never name. `{"link_state": "present", "link_state": "absent"}` becomes the answer that
    LICENSES `ip link delete` on an interface whose recorded prior state also says the panel did not
    create it. A duplicate key is how a hand-edited database, a merged backup document or a truncated
    concatenation presents, and no writer here can produce one — so it is not a record, and the safe
    answer is the one the read side already gives: no answer, retained, still covered.
    """
    def one_value_per_key(pairs):
        seen: set[str] = set()
        for key, _ in pairs:
            if key in seen:
                raise RecordUnknown(
                    f"the record names {key!r} more than once, so what it says about it cannot be "
                    "read in full")
            seen.add(key)
        return dict(pairs)

    return json.loads(raw, object_pairs_hook=one_value_per_key)


def _checked_prior_link_state(record: dict) -> str:
    """THE ONE prior-link observation the record makes, folded into a probe answer. Raises.

    A RECORD-LEVEL RULE, not two field checks, and that is the finding. `link_state` and the
    superseded `link_existed` are two spellings of the same observation and were validated
    independently, so `{"link_state": "absent", "link_existed": true}` passed both — while saying two
    contradictory things about the only question that can authorise `ip link delete`. The reader then
    preferred `link_state`, which is to say it preferred the spelling that authorises the delete over
    the one that says the link was already there. So a record carrying BOTH says it twice and is not
    one the panel wrote (each release wrote exactly one), which here means unknown: nothing acts on
    it, it is retained, and the interface it names stays covered.

    The fold itself is unchanged and is the reason the legacy spelling can never authorise anything:
    that bool had already collapsed "could not tell" into False, so False reads as `LINK_UNKNOWN` and
    only `True` reads as `LINK_PRESENT`. Neither is `LINK_ABSENT`, and `LINK_ABSENT` is the only
    answer that proves the pass created the link. One upgrade loses one cleanup opportunity; the
    other direction loses an interface.
    """
    state = _checked_link_state(record)
    legacy = _checked_flag(record, "link_existed")
    if "link_state" in record and "link_existed" in record:
        raise RecordUnknown(
            f"the record says the link's state before the change was {state or 'blank'!r} and also "
            f"records the superseded link_existed={legacy!r}, so which observation was taken cannot "
            "be read")
    if state:
        return state
    if legacy is True:
        return LINK_PRESENT
    return LINK_UNKNOWN


def _refuse_resolved(record: dict) -> None:
    """`resolved` is the CLEAR's word and no live candidate may carry it. Raises if one does.

    Reserved for the terminal form `clear_provision_candidate` writes (see `_settled_undo`) and,
    until now, ignored by the validator every writer and every direct reader shares. So
    `record_provision_candidate` would accept `{"resolved": true}` as a pending record — written and
    proven, looking to its caller like a successful arm, and read by every reader below as ALREADY
    SETTLED: no cover, no undo, and an interface a pass is about to create named nowhere. A mixed
    record is the other half of the same gap: the boot reader refuses it, while the two in-process
    rollbacks hand their dict straight to the undo, which never looked at the key at all.

    One word, one meaning, one writer.
    """
    if "resolved" in record:
        raise RecordUnknown(
            f"the record claims to be a settled undo (resolved={record['resolved']!r}) and is being "
            "read as a live candidate; only a clear may write that word")


def _checked_candidate_fields(record: dict, iface: str) -> Candidate:
    """The record as a `Candidate`: everything but `iface` proven, and the RECORD-LEVEL INVARIANTS
    checked together. Raises naming the first thing that is not what the panel writes.

    `iface` is passed in already proven, because the two halves are worth different things: an
    interface that reads is COVERAGE and is kept even when the rest of the record cannot be read,
    while the rest of the record is what an undo would act on and is worth nothing without all of it.

    THE THREE INVARIANTS ARE HERE AND NOWHERE ELSE, so every reader and the writer get all three:
    no `resolved` on a live candidate, one prior-link observation rather than two contradictory
    spellings, and — through `Candidate.names_candidate`, which its caller may only consult
    afterwards — a blank `iface` read as "no candidate" only in a record that otherwise conforms.
    """
    _refuse_resolved(record)
    return Candidate(iface=iface,
                     addr4=_checked_addr(record, "addr4"),
                     addr6=_checked_addr(record, "addr6"),
                     # Only a real `True` is a claim; `_checked_flag` has already refused everything
                     # that is neither a bool nor `null`, so the fold cannot invent one.
                     vlan=_checked_flag(record, "vlan") is True,
                     prior=_checked_prior_link_state(record))


def _checked_candidate(record: dict) -> Candidate:
    """The whole candidate record proven and decoded into the one type its readers use."""
    return _checked_candidate_fields(record, _checked_candidate_iface(record))


def _as_candidate(record) -> Candidate | None:
    """Whatever a caller hands over, as a `Candidate` or as no candidate at all. Raises.

    THE ONE DOOR INTO THE TYPE for the paths that do not read the store: the two in-process rollbacks
    pass the dict they built, the boot path passes the object it already decoded, and `{}`/`None` is
    the settled "this pass puts nothing anywhere". Anything else goes through the same decode every
    reader shares, so no path can act on a record a different path would refuse.
    """
    if record is None or isinstance(record, Candidate):
        return record
    if not record:
        return None
    if not isinstance(record, dict):
        raise RecordUnknown(f"it is {record!r}, which is not a record")
    return _checked_candidate(record)


def _merge_cover(exclude: str, sources) -> Cover:
    """Every source's names, minus `exclude`, plus a reason per source that could not answer.

    One unreadable source does not discard the others: what it costs is the RIGHT TO NARROW, not the
    names the readable ones gave. So each is asked separately and an unknown one contributes its
    reason instead of an empty list.

    A source that answers with something that is not an interface name is UNKNOWN, not answered —
    see `_checked_names`. That keeps the two halves of the cover honest together: an entry nothing
    can enforce on may not arrive as a name, and it may not arrive as silence either.
    """
    names: list[str] = []
    unknown: list[str] = []
    for source in sources:
        try:
            names.extend(_checked_names(source()))
        except RecordUnknown as exc:
            unknown.append(str(exc))
    return Cover(_other_ifaces(names, exclude), unknown)


def _ownership_cover(store) -> list[str]:
    """The interfaces the OWNERSHIP RECORDS name as possibly holding a segment address, read through
    the boundary that decodes them. Raises `RecordUnknown` only when they could not be READ.

    THE SAME `Ownership` EVERY OTHER OWNERSHIP READER GETS, and that is the whole reason this source
    exists rather than the two raw reads it replaces. Read as raw text — `managed_segment_iface`
    straight out of the record and the stale ledger straight out of `_parse_stale` — this source
    answered "no answer at all" to two states the boundary answers precisely: a SCALAR the panel did
    not write, and a ledger entry it must keep VERBATIM. Both are durable, neither can be healed by a
    pass that trips over it before it starts, and an unknown cover is what `_provision_segment`
    declines on — so one hand-edited value, or one retained entry, refused every provisioning pass for
    ever. That is the outage the invalid-versus-blank distinction exists to prevent (see `Ownership`),
    reached through the one reader that was still re-deriving the records for itself.

    So each of the three states contributes what it actually knows:

      * `unreadable` — the store would not answer, or a ledger entry could not be read IN FULL. It is
        PROPAGATED, and it is the only thing here that makes the cover short: the set of interfaces
        this would name has something already dropped out of it, and narrowing on that is the bypass.
      * `invalid` — a scalar holding something that is not an interface name. It names no interface, so
        it contributes no name; it can also enforce on nothing, so it blocks nothing. The pass goes on,
        rewrites the scalar and heals it, exactly as `reconcile_segment_addresses` already relies on.
      * `retained` — a ledger entry the panel keeps as it came. Its interface is validated on its OWN,
        so a pair retained because its ADDRESS could not be read still keeps its interface covered —
        which is the entry's whole purpose — while one whose interface is the unusable half contributes
        nothing and stops nothing. Either way the entry itself is untouched and still reported where it
        is read.
    """
    own = _read_ownership(store)
    if not own.known:
        raise RecordUnknown("the panel's record of what it owns on the segment could not be read in "
                            "full: " + "; ".join(own.unreadable))
    names = [own.iface] + [iface for iface, _ in own.stale]
    for iface, _ in own.retained:
        try:
            names.extend(_checked_names([iface]))
        except RecordUnknown:
            continue            # text nothing can be enforced on, and not a reason to refuse a pass
    return names


def superseded_state(store, new_iface: str) -> Cover:
    """Every interface OTHER than `new_iface` on which the panel still holds a segment address, and
    whether that is the WHOLE answer.

    Three records answer this and all three are needed. `managed_segment_iface` is the interface
    the last pass configured, which is the one every rule the panel installed still names —
    including on a host where the panel created neither link (an operator's own interfaces,
    retargeted between). The link ledger adds any VLAN the panel created and has not yet retired,
    which is where an earlier FAILED retarget leaves a second live interface. The STALE ledger adds
    the rest, and it is the one that was missing: `reconcile_segment_addresses` rewrites
    `managed_segment_iface` to the candidate BEFORE it touches the kernel, so an `ip addr replace`
    the kernel then refuses leaves the working old interface — up, still carrying the segment
    address the pass meant to supersede — represented by a stale pair and by nothing else. On a
    host where the panel created neither link, that pair is the only record of it that exists.

    This is what the panel MANAGES elsewhere: the NetworkManager drop-in is written from it, and
    handing one of these interfaces back to NM while the panel still holds an address on it invites
    NM to reconfigure an interface that is still in service. The ruleset must cover more than this
    (see `enforcement_cover`) — naming an interface in a rule costs nothing, while un-managing one
    the panel never addressed takes it away from the thing whose job it is.

    TWO OF THE THREE ARE READ THROUGH `_read_ownership`, and never again out of the scalar and the
    ledger themselves. A typed boundary only protects the readers that use it, and this one used to
    re-derive its half: the interface scalar was read raw, so a value the panel did not write made the
    whole cover UNKNOWN, and the stale ledger was read all-or-nothing, so one entry the boundary would
    have retained discarded the valid pairs beside it. Unknown here is what the pass declines on, and
    both of those states survive the pass that reads them — which turned "one refused change, then a
    heal" into provisioning refused for ever. So they arrive already decoded, and only "the record
    could not be READ" is short (see `_ownership_cover`).

    Read BEFORE `reconcile_segment_addresses` when the answer is "what is this pass moving off":
    afterwards the record names where it is moving TO. Read again after, to ask what is left.

    RETURNS A `Cover`, AND THERE IS NO NAMES-ONLY VERSION OF IT ANY MORE. There was, and every
    decision this record feeds turned out to be one the names alone cannot carry. A RENDER may
    install `.names` — widening a ruleset or a drop-in is safe, so an unreadable source contributing
    nothing costs nothing there. Everything else here is a decision about whether to STOP covering
    something, or whether there is anything to cover at all: whether the pass is a move and so stages
    the transitional ruleset, which interfaces the drop-in takes from NetworkManager, whether the
    enforcement may narrow back. Under the names alone a source the store would not read arrives at
    each of them as "the panel holds nothing anywhere else", which is the direct-WAN bypass produced
    by a store read. So `.known` travels with the names and the callers read it (see `Cover`).
    """
    return _merge_cover(new_iface, [
        lambda: _ownership_cover(store),
        lambda: _parse_links(store),
    ])


def enforcement_cover(store, iface: str) -> Cover:
    """THE DURABLE COVER: every interface other than `iface` the ruleset must also name.

    Enforcement is scoped by interface name and the store names exactly ONE segment interface, so
    a ruleset rendered from the store alone describes the host only when the host has one segment
    interface. The pass performing a MOVE knows both names and covers both for the length of the
    move — but the move is not the only way a second interface ends up live, and the others outlast
    the pass that created them. An address replacement the kernel refuses leaves the old interface
    up and addressed with the ownership record already pointing at the candidate; a rolled-back
    change leaves a candidate interface the undo may not delete (it is the operator's own, or its
    removal was refused) still up and carrying the address the pass put there. Both survive into
    every LATER render — the next `sync_net`, the next boot — and a render that narrows to the
    configured interface while one of them is still up is not a cosmetic mismatch: it is the
    direct-WAN bypass, for as long as the interface lasts.

    So the cover is durable, is derived from every record of an interface that may be up carrying a
    segment address, and is consumed by every path that renders enforcement from the store:
    `apply_net` and `stop_net` (and so `sync_net`, `boot_guard`, the two rollback restores and
    every other caller of those two), and the provisioning pass, which stages it before it raises
    anything and narrows only when this returns empty.

    AN INTERFACE LEAVES ONLY WHEN ITS ABSENCE IS PROVEN, and each source proves it its own way:
    the ownership record is rewritten by a reconcile that worked, a link entry is dropped when the
    delete is proven (`_delete_owned_link`), a stale pair when the removal is proven
    (`_retire_owned_addr`), and a surviving candidate when the host says its link is gone or its
    address is not on it (`drain_enforcement_cover`). Every one of those is an explicit answer from
    the host; "could not tell" keeps the interface covered. Covering an interface that is already
    gone costs nothing — nft matches `iifname` by name, so a rule naming a device that is not there
    simply never matches — while uncovering one that is still up is the defect.

    THE PENDING UNDO RECORD IS THE FALLBACK UNDER ALL OF THAT, for the window in which a ledger
    entry has been DECIDED and cannot be shown to be written: a store that refuses the write, or
    accepts it and keeps nothing, would otherwise leave the undo's own decision to leave an
    interface live recorded nowhere. It names the one interface a pass may have put the segment on,
    it is already on disk before the pass starts, and it is cleared exactly when the undo has
    nothing left outstanding — which now includes the survivor write being read back (see
    `pending_candidate_ifaces` and `remember_survivors`). So it covers the gap and then gets out of
    the way, rather than becoming a second ledger nothing can drain.

    RETURNS A `Cover`, NOT A LIST, and that is what the type is for: five records answer this and any
    of them can fail to answer at all. A caller that RENDERS installs `.names`, because names are
    all a ruleset can hold; a caller that NARROWS must ask `.may_narrow`, which is true only when
    every source answered and none of them named anything.
    """
    survivors = _merge_cover(iface, [
        lambda: [name for name, _ in _parse_survivors(store)],
    ])
    superseded = superseded_state(store, iface)
    pending = pending_candidate_state(store)
    return Cover(_other_ifaces(superseded.names + survivors.names + pending.names, iface),
                 superseded.unknown + survivors.unknown + pending.unknown)


def covering_plan(plan: NetPlan, extra: list[str]) -> NetPlan:
    """`plan`, with the segment rules scoped to the interfaces it is being moved off as well."""
    return replace(plan, extra_ifaces=tuple(extra))


def apply_enforcement(state, plan: NetPlan) -> str:
    """Install the segment enforcement for `plan`; return "" or why it did not go on.

    WHICH ruleset the host gets — full tproxy, the fail-closed guard, or a teardown — is the
    controller's decision and stays there, so the transitional ruleset and the one that replaces
    it can never be of different kinds. The import is deferred for the same reason the apply
    lock's is: the controller reaches into this module too, and neither may import the other at
    module scope. The lock is already held by every caller here.
    """
    from pi_gw_panel.controller import sync_net_plan
    try:
        result = sync_net_plan(state, plan)
    except Exception as exc:
        _log.error("the segment enforcement could not be applied: %s", exc)
        return f"the segment enforcement could not be applied: {exc}"
    if not getattr(result, "ok", False):
        return getattr(result, "error", "") or "the segment enforcement could not be applied"
    return ""


# --- the fallback under the cover: enforcement that needs no interface name ---------------------
#
# Everything above refuses rather than installs a ruleset it cannot prove covers every interface the
# segment may be on, and that refusal is right: a ruleset is REPLACED, so a short one uncovers a
# live interface. But refusing has an outcome as well as a virtue, and at BOOT the outcome is the
# defect. nft rules live in the kernel, so a panel restart inherits the previous correct ruleset and
# a refusal costs nothing — while a HOST reboot arrives with no table at all, and `ensure_sysctls`
# turns forwarding on. A refusal there leaves the forward path physically open. Readiness reports it
# red, and readiness is observability: no packet consults `/api/ready`.
#
# So there is a second, weaker kind of enforcement underneath, for exactly the state in which the
# normal kind cannot be rendered: a FORWARD deny that NAMES NO INTERFACE. That is not a smaller
# version of the ruleset above, it is the only shape available — the whole reason the render refused
# is that the panel cannot say which interfaces to name, so anything that has to name one is out.
# A base chain with `policy drop` and no rules needs nothing to be known about the host.
#
# IT CANNOT LOCK THE OPERATOR OUT, and that is a property of the hook, not of a carve-out that could
# be got wrong. It is registered on `forward` only, which is the hook TRANSIT packets traverse.
# Traffic addressed to this machine — the panel over the management leg, SSH, and the DHCP/DNS the
# segment's own clients ask this host for — goes `prerouting` -> `input` and never enters `forward`;
# traffic the host originates, including the tunnel's own connection to the node, goes `output` ->
# `postrouting`. None of it is reachable from a forward chain, so the machine stays reachable on
# every leg while every forwarded packet is dropped. What IS dropped is other transit through this
# host, a Docker bridge's egress included; that is the accepted cost of fail-closed, and it is the
# same cost `ip_forward=0` would have.
#
# IT IS A SEPARATE TABLE from `pi_gw_panel`, deliberately. The normal ruleset is loaded by deleting
# and recreating its own table, so a deny living inside it would be removed by the very apply that
# may not have covered everything — and would be indistinguishable from the enforcement it stands in
# for. A table of its own is removed only by the handover below, and only once the panel's own
# enforcement is confirmed on the host.
EMERGENCY_TABLE = "pi_gw_panel_emergency"

# The note lives on the net backend, beside `enforcement_status`/`wan_blocked`: the same long-lived
# object, holding the same kind of fact — what is CONFIRMED about the host right now — and, like
# them, deliberately not persisted, because a fresh process has confirmed nothing. It carries BOTH
# shapes of the fallback state, because both matter and the urgent one is the second: the deny is in
# force, or it could not be installed and nothing is known to be holding the forward path.
_EMERGENCY_ATTR = "enforcement_fallback_note"

# One transaction per family. `add` makes the following `delete` safe on a host that has never had
# the table, and the recreate is inside the SAME `nft -f`, so the hook is never momentarily
# unregistered — the identical idempotent shape the normal ruleset is loaded with.
_EMERGENCY_SCRIPT = "".join(
    f"add table {family} {EMERGENCY_TABLE}\n"
    f"delete table {family} {EMERGENCY_TABLE}\n"
    f"table {family} {EMERGENCY_TABLE} {{\n"
    "    chain forward {\n"
    "        type filter hook forward priority filter; policy drop;\n"
    "    }\n"
    "}\n"
    for family in ("ip", "ip6"))


def enforcement_fallback_note(net) -> str:
    """What is holding the forward path while the panel's own enforcement is not. Never raises.

    "" means the ordinary state: the enforcement the panel renders is the thing on the host. Anything
    else is a sentence for the operator, and it says which of the two fallback states this is — the
    emergency deny in force, or the deny itself refused.
    """
    return getattr(net, _EMERGENCY_ATTR, "") or ""


def _record_emergency(net, reason: str) -> None:
    """Park the note on the backend, exactly as the controller parks its enforcement snapshot."""
    setattr(net, _EMERGENCY_ATTR, reason)


def _disable_forwarding(write_proc=None) -> str:
    """Turn IPv4 and IPv6 forwarding off. "" or why one of them is not proven off.

    THE LAST RESORT, AND NOT A SUBSTITUTE for the deny above. It is interface-independent for the
    same reason, but it is contested state: `ensure_sysctls` and `LinuxBackend._ensure_forward` write
    it back to 1, so anything the panel does afterwards can undo it, while an nft table stays until
    something deletes it. It exists to narrow the window in the one case where the table could not be
    loaded at all — and in that case boot does not go on to run the things that would rewrite it.

    The writer is resolved at CALL time, never bound as a default: on a host running the suite as
    root a default-bound `_write_proc` would take the test's forwarding down with it.
    """
    write = write_proc or _write_proc
    failed: list[str] = []
    for path in ("/proc/sys/net/ipv4/ip_forward", "/proc/sys/net/ipv6/conf/all/forwarding"):
        if write(path, "0") is False:
            failed.append(path)
    return ("forwarding could not be turned off either (" + ", ".join(failed) + ")"
            if failed else "")


def install_emergency_forward_deny(state, why: str, write_proc=None) -> str:
    """Hold every forwarded packet closed without naming an interface. "" when it is PROVEN in
    force; otherwise the reason nothing can be shown to be holding it.

    A caller that gets a reason back has learned that the packet path is not accounted for by
    anything, which is the one state boot may not carry on through (see `app.create_app`).
    """
    run = getattr(state.net, "_run", None)
    if run is None:
        # No host: the dev/CI backends render and mutate nothing, so there is no forward path to
        # hold and nothing to report. The same gate `host_provision` opens with.
        return ""
    try:
        run(["nft", "-f", "-"], input=_EMERGENCY_SCRIPT)
        for family in ("ip", "ip6"):
            run(["nft", "list", "table", family, EMERGENCY_TABLE])
    except (subprocess.CalledProcessError, OSError) as exc:
        detail = (getattr(exc, "stderr", None) or str(exc) or exc.__class__.__name__).strip()
        sysctl = _disable_forwarding(write_proc)
        reason = (f"the emergency deny on forwarded traffic could not be installed ({detail}); "
                  + (sysctl or "IPv4 and IPv6 forwarding were turned off instead, which the next "
                               "host pass would undo")
                  + f" — the enforcement it stands in for was refused because: {why}")
        _record_emergency(state.net, reason)
        _log.critical("%s", reason)
        return reason
    reason = ("every forwarded packet is being dropped by an interface-independent emergency deny, "
              "because the panel's own segment enforcement could not be installed: " + why
              + " — segment clients have no network until it can be, and traffic to this host "
                "(the panel, SSH, the segment's DHCP and DNS) is unaffected")
    _record_emergency(state.net, reason)
    _log.critical("%s", reason)
    return ""


def release_emergency_forward_deny(state) -> str:
    """Remove the emergency deny. "" when it is gone; otherwise why it may still be dropping.

    Called only where the panel's own enforcement has been CONFIRMED on the host, and then called
    unconditionally — the marker is in-memory and the table is in the kernel, so a process that
    installed the deny and was restarted arrives with nothing recorded and a table that would drop
    every forwarded packet for ever. "No such table" is therefore the success answer, not an error,
    and the delete is attempted on every confirmation.
    """
    run = getattr(state.net, "_run", None)
    if run is None:
        _record_emergency(state.net, "")
        return ""
    failed: list[str] = []
    for family in ("ip", "ip6"):
        try:
            run(["nft", "delete", "table", family, EMERGENCY_TABLE])
        except subprocess.CalledProcessError as exc:
            err = ((exc.stderr or "") + " " + (exc.stdout or "")).lower()
            if not any(token in err for token in _LINK_ABSENT_TOKENS):
                failed.append(f"{family}: {(exc.stderr or str(exc)).strip()}")
        except OSError as exc:
            failed.append(f"{family}: {exc or exc.__class__.__name__}")
    if failed:
        reason = ("the segment enforcement is installed, but the emergency deny on forwarded "
                  "traffic could not be removed, so segment clients may still have no network: "
                  + "; ".join(failed))
        _record_emergency(state.net, reason)
        _log.error("%s", reason)
        return reason
    _record_emergency(state.net, "")
    return ""


def enforcement_fallback(state, result) -> str:
    """Account for the forward path given the outcome of the panel's own enforcement.

    "" means something is holding it — either the enforcement that was just confirmed, or the
    emergency deny, proven in force. A reason means NOTHING can be shown to be holding it, and that
    is the answer a caller may not proceed through.

    A confirmed enforcement releases the deny, and a release that fails is reported but is NOT such
    an answer: what it leaves behind drops forwarded traffic, which costs the segment its network and
    leaks nothing. Failing closed is never the failure this function exists to catch.
    """
    if getattr(result, "ok", False):
        release_emergency_forward_deny(state)
        return ""
    why = (getattr(result, "error", "")
           or "the panel could not install the segment enforcement, and gave no reason")
    return install_emergency_forward_deny(state, why)


def _hand_back_from_emergency_deny(state) -> str:
    """Replace the emergency deny with the panel's own enforcement, once that can be rendered.

    "" when nothing is in force or the handover is complete; otherwise why the deny still is.

    THIS IS THE RECOVERY, and it lives on the provisioning pass because that is what every path
    which can change the answer runs — boot, `PUT /api/network`, a restore. The deny names no
    interface, so nothing narrows it and nothing drains it: the only thing that can end it is a
    store-derived render that PROVES a complete cover, which is exactly what the render refuses to
    do otherwise. So the render is asked for, and only one that reached the host releases the deny; a
    render that refuses again leaves it exactly where it is, says so through the pass result, and the
    next pass asks again.
    """
    if not enforcement_fallback_note(state.net):
        return ""
    from pi_gw_panel.controller import sync_net
    try:
        applied = sync_net(state)
    except Exception as exc:
        applied = NetResult(ok=False, error=str(exc) or exc.__class__.__name__)
    if not getattr(applied, "ok", False):
        return ("forwarded traffic is still being dropped by the emergency deny, because the "
                "panel's own segment enforcement still could not be installed: "
                + (getattr(applied, "error", "") or "no reason given"))
    why = release_emergency_forward_deny(state)
    if why:
        return why
    _log.warning("the segment enforcement is installed again; the emergency deny on forwarded "
                 "traffic has been removed")
    return ""


def retire_superseded_links(store, plan: NetPlan, run=_run, link_exists=None) -> list[str]:
    """Delete the panel-created VLANs the current plan no longer wants. Call this ONLY once the
    plan's addresses are on the interface.

    A link is "superseded" the moment the segment it carried has been replaced by one that
    WORKS, and not one step earlier. It used to be retired inside `ensure_segment_link`, before
    the addresses were reconciled, and that ordering is the defect this function exists to end:
    a retarget whose `ip addr replace` is then rejected — EPERM, a bad prefix, a busy address,
    a kernel that will not take it — had already lost the old VLAN and everything on it, and the
    `applied=False` gates downstream have nothing left to preserve. The operator's gateway is
    left with the old link deleted and the new one unaddressed: no segment network at all.

    So the caller runs this after the reconcile reports `applied`, and a failed retarget simply
    does not reach it. What that leaves behind is exactly right: the OLD link is still the live
    one, still carrying its address, and it is still in the ledger — which therefore still
    describes the host, now naming both links, both of which really are on it and really were
    created by this panel. The next pass retries the addresses and retires the old link the
    moment they land.

    Each superseded link is dropped from the ledger only once its removal is proven, and every
    link that could not be retired is returned as a reason string so the caller reports a
    provisioning failure rather than success over a link it owns and left running.
    """
    owned, unreadable = _read_links(store)
    if owned is None:
        # Not "there is nothing to retire": a ledger that cannot be read in full may name a link
        # this panel created and has to delete — and a mangled entry is exactly a link it cannot
        # name. Nothing is touched and the pass reports, so a later one retries.
        return ["the panel-created VLAN ledger could not be read, so no link was retired: "
                + unreadable]
    superseded = [name for name in owned if name != plan.segment_iface]
    if not superseded:
        return []
    return _retire_links(store, superseded, owned, run, _probe_seam(link_exists, run))


def clear_managed_link(store, run=_run, link_exists=None) -> list[str]:
    """Delete every VLAN link this panel created, forgetting each one its removal is proven.
    Returns a reason per link that is not proven gone from the host afterwards."""
    owned, unreadable = _read_links(store)
    if owned is None:
        return ["the panel-created VLAN ledger could not be read, so no link was removed: "
                + unreadable]
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

# The ledger is a retry list, and every DISTINCT address whose removal is refused stays on it.
# Nothing about that needs an operator: a DHCPv6-PD prefix can renew repeatedly on its own, and each
# renewal that cannot retire the address it supersedes adds an entry. Left alone it grows the
# persisted state and the delete-plus-probe loop that runs under the apply lock on every pass.
#
# That growth used to be CAPPED, and the cap worked by REFUSING the operator's change once the panel
# owned too much. It is gone, deliberately. A refusal is a "do nothing" branch in the middle of a
# multi-step host reconfiguration, so every other step has to be ordered correctly against it — and
# twice it was not, both times in the direction the cap existed to prevent. `ensure_segment_link`
# retired the superseded VLAN before the addresses were reconciled (it no longer does; that is what
# `retire_superseded_links` is for), so an interface retarget at the cap deleted the old link and
# its address and then declined to install the new one, leaving the segment with neither — a refusal
# was only ONE way to reach that state, a rejected `ip addr replace` was another, and the ordering
# itself was the defect; and the refusal's own preflight rewrote the ledger from a list the desired
# pairs had already been subtracted from, dropping an address on the host out of both the stale
# record and the current-address keys. Weighed honestly, a backlog costs rows in a settings table
# and a slow loop, and needs repeated FAILED deletions to reach; a half-applied pass costs a live
# gateway its network. A pass may never decline to apply the operator's change on these grounds.
#
# So growth is made VISIBLE instead of fatal. Past `BACKLOG_WARN` retained pairs the pass names the
# count through the same result that fails `/api/ready`'s `provisioning` check, and otherwise
# behaves exactly as it does at one entry: it applies the change, retries every entry, and forgets
# none. The number is a diagnostic and not a limit — one rotation supersedes at most two addresses
# (v4 + v6), so a backlog this size is four rotations' worth of removals that ALL failed, which is a
# host problem the panel cannot fix by itself and an operator has to be told about. It is chosen to
# sit above anything normal operation produces and far below anything that hurts; nothing whatsoever
# behaves differently at the boundary, which is the point.
BACKLOG_WARN = 8


def _parse_pairs(store, key: str) -> list[tuple[str, str]]:
    """`(iface, addr)` pairs from a set record, in order.

    Raises `RecordUnknown` when the store cannot answer AND when what it answered cannot be read in
    full — see `Record` for why neither may come back as an empty ledger. The cover sources want
    exactly that: `_merge_cover` turns the raise into a reason that forbids narrowing.
    """
    return read_set(store, key).pairs()


def _read_pairs(store, key: str) -> tuple[list[tuple[str, str]] | None, str]:
    """The pairs at `key`, or `(None, reason)` when the record cannot be read IN FULL. Never raises.

    For the callers that may not raise and may not narrow: an unreadable record and one holding an
    entry that is not a pair are ONE answer to every one of them, because both mean the set they
    would compute is a set with something already dropped out of it. Written as a returned reason
    rather than an exception because each of those callers reports it its own way — keep the ledger
    as it is, keep the pending record, install nothing — and none of them has anything to gain from
    a traceback (see `remember_survivors`, `drain_enforcement_cover`, `_read_ownership`).
    """
    record = read_set(store, key)
    if not record.known:
        return None, record.reason
    try:
        return record.pairs(), ""
    except RecordUnknown as exc:
        return None, str(exc)


def _parse_stale(store) -> list[tuple[str, str]]:
    """`(iface, addr)` pairs recorded as panel-owned but not yet removed from the kernel."""
    return _parse_pairs(store, STALE_KEY)


# --- the surviving-candidate ledger (the part of the cover the undo cannot settle) -------------
#
# The stale ledger above answers for addresses the panel OWNS. This one answers for the addresses
# it does not: a rolled-back change whose candidate interface is still on the host. The undo
# deletes a candidate LINK only when it can prove the pass created it, and names-and-leaves every
# address (see `_report_orphan_addrs`), so its ordinary outcome on an operator's own interface —
# and on a link whose deletion was refused — is "it is still there, and it may be carrying the
# segment address this change put on it". That is precisely an interface the ruleset has to name,
# and until this ledger existed nothing recorded it: the undo reported the orphan once, cleared the
# pending record, and the guard the rollback installed straight afterwards was rendered from a
# store that names only the interface it went back to.
#
# It is a separate key from the pending undo, because the two answer different questions and clear
# on different proofs. The pending record asks "is there work a later pass could still finish", and
# an orphan nothing will ever delete is deliberately NOT that — keeping it would repeat the same
# message forever. This one asks "may that interface still be carrying the segment", which stays
# true exactly as long as the host says so, and is answered by probing (`drain_enforcement_cover`).
#
# The two do meet in one place. Writing to THIS ledger is itself work a later pass can finish, so a
# write that cannot be proven to have landed makes the undo unresolved and keeps the pending record
# — which then covers the interface on its own until the write succeeds (`pending_candidate_ifaces`).
# That is the only way the coverage decision can outlive a store that will not take it.
SURVIVOR_KEY = "provision_candidate_survivors"


def _checked_survivors(pairs: list[tuple[str, str]], where: str) -> list[tuple[str, str]]:
    """Every survivor proven to be an interface name and an address, and returned in the ONE spelling
    the host prints. Raises `RecordUnknown` otherwise.

    `Record.pairs()` proves an entry is two tokens; this proves what the two tokens SAY, and it is
    the half that decides coverage. A bogus entry is not an interface the ruleset over-covers: it
    names a device that cannot exist and an address the host can never print, so the very first
    drain gets an explicit not-found for it and the interface LEAVES the enforcement — which is how
    a real segment address still on that interface ends up outside the ruleset. So a ledger holding
    one is not read as a ledger at all (nothing is drained from it, nothing is shrunk) and a pair
    that is not one never goes in (see `remember_survivors`, `drain_enforcement_cover`).

    IT RETURNS THE CANONICAL PAIRS and its callers use them, which is the other half of the same
    property: an entry the drain cannot match against `ip addr show` drains itself on the first probe
    just as surely as a bogus one, so the entries this ledger keeps are the spelling the comparison
    speaks (see `_checked_addr_text`). What the probe compares and what the record carries are then
    the same value by construction.
    """
    out: list[tuple[str, str]] = []
    for iface, addr in pairs:
        _checked_names([iface])
        out.append((iface, _checked_addr_text(addr, f"{where} on {iface}")))
    return out


def _survivor_pairs(store) -> tuple[list[tuple[str, str]] | None, str]:
    """The ledger's pairs, proven to be interface names and canonical addresses, or `(None, reason)`.

    `_read_pairs` with the entries' CONTENT checked as well as their shape — one answer for the three
    callers that may neither raise nor narrow, for the reason `_read_pairs` gives: a set they would
    compute from this is a set with something already dropped out of it.
    """
    pairs, unreadable = _read_pairs(store, SURVIVOR_KEY)
    if pairs is None:
        return None, unreadable
    try:
        return _checked_survivors(pairs, "the address a rolled-back change may have left"), ""
    except RecordUnknown as exc:
        return None, str(exc)


def _parse_survivors(store) -> list[tuple[str, str]]:
    """`(iface, addr)` pairs a rolled-back change may have left live on a candidate interface.

    Raises `RecordUnknown` when the ledger cannot be read IN FULL — the store would not answer, an
    entry is not a pair, or a pair is not an interface and an address. For the cover source this
    feeds that is the reason that forbids narrowing (`_merge_cover`), which is the answer every one
    of those three deserves.
    """
    pairs, unreadable = _survivor_pairs(store)
    if pairs is None:
        raise RecordUnknown(unreadable)
    return pairs


def _record_survivors(store, pairs: list[tuple[str, str]]) -> str:
    """Replace the ledger with exactly `pairs`, proven. "" or why it does not name them all."""
    return write_set(store, SURVIVOR_KEY, [f"{iface} {addr}" for iface, addr in pairs])


def remember_survivors(store, pairs: list[tuple[str, str]]) -> bool:
    """Add `pairs` to the surviving-candidate ledger; return whether it PROVABLY names them all.

    Additive because a second rollback does not settle the first one's leftovers, and because the
    pairs a single undo reports are the ones it has just decided NOT to remove.

    THE ANSWER MAY NOT BE INFERRED FROM THE WRITE, and it used to be: this returned nothing and
    swallowed every failure, so the caller carried on as though durable coverage existed. A
    `set_setting` can raise, and it can also return having written NOTHING — and in both cases the
    ledger does not name the interface the undo has just decided to leave up and addressed, while
    the caller's very next act is to render the restored guard from that ledger. "No exception"
    read as "written" is therefore the direct-WAN bypass with a record claiming otherwise.

    ADDITIVE MEANS THE WHOLE SET IS AT STAKE, not just `pairs`, and that is the second half of the
    same mistake. This replaces the record with `existing + want`, so it can only be verified against
    the COMPLETE expected content: checking that `want` came back passes a partial write that keeps
    the pair the undo just decided about and drops an OLDER survivor — permanently, because nothing
    asks for that one again and the pending record retained below names only the current candidate.
    So both halves live in `write_set`, which journals the whole set before replacing the record and
    verifies every entry afterwards — and leaves the journal in place when it cannot, so the older
    pair stays covered rather than merely being reported missing. An UNREADABLE record is not
    replaced at all, for the same reason: a set computed from a read that failed is a set with the
    older survivors already dropped out of it.

    STILL NEVER RAISES. This runs on a recovery path already handling a failure, and the caller has
    something better to do with `False` than an exception: it marks the undo unresolved, which keeps
    the pending record so a later pass retries this write, and the enforcement covers the candidate
    from that record in the meantime (see `pending_candidate_ifaces`). Nothing is lost silently and
    nothing is uncovered while it is lost.
    """
    want = _distinct(list(pairs))
    if not want:
        return True
    try:
        # NOTHING BOGUS GOES IN, whatever a caller hands over, and what does go in is the one
        # spelling the drain compares. The entries here become the names the enforcement carries and
        # the tokens the drain proves gone, so a pair that cannot be either — or one the host can
        # never print back in the same letters — is a survivor that drains itself on the first probe
        # and takes a live interface's coverage with it. Refused the same way an unprovable write is:
        # `False`, so the undo stays pending and the cover names the candidate from that record
        # instead (`pending_candidate_ifaces`).
        want = _checked_survivors(want, "the address a rolled-back change may have left")
    except RecordUnknown as exc:
        _log.error("the interfaces a rolled-back change may have left carrying the segment were not "
                   "recorded, because what was offered is not an interface and an address: %s", exc)
        return False
    held, unreadable = _survivor_pairs(store)
    if held is None:
        _log.error("the surviving-candidate ledger could not be read in full, so it was not "
                   "replaced — replacing a record that cannot be read is how an older survivor is "
                   "lost. The interfaces stay covered through the pending undo record: %s",
                   unreadable)
        return False
    why = _record_survivors(store, _distinct(held + want))
    if why:
        _log.error("could not record the interfaces a rolled-back change may have left carrying "
                   "the segment; they stay covered through the pending undo record and the "
                   "journalled ledger: %s", why)
        return False
    return True


def forget_survivors(store, iface: str) -> None:
    """Drop every recorded survivor on `iface`, which the caller has PROVEN is gone from the host.

    One caller: the undo, after an `ip link delete` it verified. A link that goes takes every
    address on it with it, so nothing on that interface can still be carrying the segment.

    Never raises, and never shrinks a record it could not read: both failures leave every pair where
    it was, which over-covers an interface that is gone — free — instead of dropping one that is not.
    """
    if not iface:
        return
    pairs, unreadable = _survivor_pairs(store)
    if pairs is None:
        _log.warning("could not read the surviving-candidate ledger in full to drop %s, so every "
                     "pair in it stays covered: %s", iface, unreadable)
        return
    keep = [pair for pair in pairs if pair[0] != iface]
    if keep == pairs:
        return
    why = _record_survivors(store, keep)
    if why:
        _log.warning("could not drop %s from the surviving-candidate ledger, so it stays covered "
                     "until the host proves it gone: %s", iface, why)


def drain_enforcement_cover(store, run=_run) -> list[tuple[str, str]]:
    """Drop every recorded survivor the HOST says is gone; keep every other one. Never raises.

    The property that stops the cover growing forever, and the one place "not proven absent" could
    quietly collapse into "absent". Two answers, and only two, take a pair out: the host says the
    LINK is not there (it took its addresses with it), or it says the ADDRESS is not on the
    interface (the operator removed it, as the undo's report asked, or the pass never installed it
    in the first place). Both are explicit not-found answers from iproute2. Everything else —
    present, refused, a netlink error, the runner's time limit, output that cannot be read — is
    `*_UNKNOWN` and KEEPS the pair, because an interface that may still be up carrying the segment
    is exactly what the cover is for.

    Never raises: it runs at the top of every pass, and a probe that will not answer must not cost
    the operator the pass that brings their segment up. An exception leaves the ledger untouched,
    which keeps everything covered — the safe direction. A LEDGER THAT CANNOT BE READ IN FULL is the
    same answer reached earlier: nothing is dropped, because nothing was proven gone. So is one
    holding an entry that is not an interface and an address: the probe would answer not-found for a
    device and a token the host can never have, and that answer is about the entry, not about
    whatever the entry was meant to name (see `_checked_survivors`).
    """
    pairs, unreadable = _survivor_pairs(store)
    if pairs is None:
        _log.warning("could not read the interfaces a rolled-back change left behind, so they stay "
                     "covered: %s", unreadable)
        return []
    try:
        if not pairs:
            return []
        keep: list[tuple[str, str]] = []
        links: dict[str, str] = {}
        for iface, addr in pairs:
            if iface not in links:
                links[iface] = _probe_link(iface, run)[0]
            if links[iface] == LINK_ABSENT:
                _log.info("%s is no longer on the host; it leaves the segment enforcement", iface)
                continue
            if _probe_addr(iface, addr, run)[0] == ADDR_ABSENT:
                _log.info("%s is no longer on %s; it leaves the segment enforcement", addr, iface)
                continue
            keep.append((iface, addr))
        if keep != pairs:
            why = _record_survivors(store, keep)
            if why:
                # The record still names pairs the host says are gone: over-covering, which is free,
                # and the next pass drains again. Reported so a store that keeps nothing is visible.
                _log.warning("the surviving-candidate ledger could not be shrunk, so it keeps "
                             "naming interfaces the host says are gone: %s", why)
        if keep:
            _log.warning("a rolled-back change may still have the segment on %s, so the "
                         "enforcement keeps naming it: %s",
                         ", ".join(sorted({iface for iface, _ in keep})),
                         "; ".join(f"{addr} on {iface}" for iface, addr in keep))
        return keep
    except Exception:
        # The ledger is left exactly as it was, so every pair in it is still covered.
        _log.warning("could not check the interfaces a rolled-back change left behind; they stay "
                     "covered", exc_info=True)
        return []


OWNERSHIP_KEYS = ("managed_segment_iface", "managed_segment_addr4", "managed_segment_addr6")


# --- what the panel owns on the segment, as a TYPE -------------------------------------------
#
# The four ownership records are the pending undo's SIBLING, and every defect the undo record grew a
# type to end lived here too, unfixed: a reader reaching a raw scalar and deriving a poorer answer
# from it. The addresses were compared as TEXT, so an expanded or upper-case IPv6 semantically equal
# to the one the pass was installing was "different", was appended to the stale ledger, escaped the
# desired-address protection — which is also a text comparison — and was `ip addr del`'d moments
# after its canonical spelling went on. The interface scalar was not checked at all, so a
# space-bearing value became an `ip addr del … dev <two names>` target and a THREE-TOKEN stale entry,
# which every later read of the ledger refuses — provisioning and clearing both blocked until
# someone edits the database.
#
# A previous round declined to validate the interface here for exactly that second reason, and the
# reasoning was sound as far as it went: retaining an invalid value writes an unparseable ledger and
# wedges the gateway, so it chose not to look. The option it did not consider is to DISTINGUISH
# INVALID FROM BLANK and act on neither. Blank is a legitimate "the panel claims nothing"; invalid
# means do not compare it, do not derive a pair from it, do not delete on its authority, report it,
# and leave what is on the host alone — while the pass itself carries on and installs the operator's
# addresses, so nothing is wedged. And the write side refuses a ledger it could not read back, which
# is what makes the unparseable state unreachable rather than merely tolerated.
#
# So the records stop being four raw scalars at the boundary. They are read once, canonicalised once
# (the same `parsed.with_prefixlen` the survivor ledger and `Candidate` use), family-checked against
# the key that names the family, and handed to their readers as an `Ownership` — which has no raw
# scalar to reach and answers the blank-versus-invalid question as a method, so no reader can
# re-derive the flattening.


@dataclass(frozen=True)
class Ownership:
    """WHAT THE PANEL OWNS ON THE SEGMENT, decoded once and true of the four records together.

    `iface`/`addr4`/`addr6` — the segment the panel currently claims, in the one canonical spelling
    and of the family their keys name, or "" where the record claims nothing USABLE. `stale` — the
    pairs owed a removal, every one of them a real interface name and a canonical address.

    And three facts about the reading itself, because "the store would not answer", "it answered with
    something the panel never wrote" and "it answered" are three different things and were one:

    `unreadable` — the store could not tell us, or a set record could not be read IN FULL. Nothing
    may be installed or removed on that basis; the ledger a pass would rewrite is one the unread
    entries have already dropped out of.

    `invalid` — a SCALAR is there and is not what the panel writes, in SHAPE or in TYPE. It authorises
    nothing: the action it would have licensed does not happen. The pass still applies the operator's
    change and then rewrites all three scalars, so the record HEALS — which is why this one may travel
    the pass's ordinary failure channel: what it costs is one refused change plus the heal that comes
    with it, not provisioning refused for ever. The type half of that arrives through `_read_scalar`;
    a store that would not answer at all is `unreadable` above, and always was.

    `retained` — ledger entries in that same state, held verbatim so the rewrite does not drop the
    only record of an address that may be on the host. They are never compared, never deleted, and
    never rebuilt from parts; they go back exactly as they came — and precisely because they go back,
    they are NOT in `invalid`. The panel cannot heal an entry it must preserve, so failing the pass on
    one would fail every later pass too, and refusing the operator's change for ever until someone
    edits the database is the outage this whole distinction exists to avoid. They are reported ONCE PER
    PASS, at its boundary and not at each of the five reads inside it (see
    `_report_retained_ownership`).

    `iface_unusable` distinguishes the two ways `iface` is "": see `iface_or`, which is the only
    place the difference is spent.
    """
    iface: str = ""
    addr4: str = ""
    addr6: str = ""
    stale: tuple[tuple[str, str], ...] = ()
    retained: tuple[tuple[str, str], ...] = ()
    unreadable: tuple[str, ...] = ()
    invalid: tuple[str, ...] = ()
    iface_unusable: bool = False

    def __bool__(self):
        """Never a truth value: a record that reads and claims nothing, and one that would not read
        at all, are both falsey and mean opposite things (the same rule as `Record` and `Cover`)."""
        raise TypeError("an ownership record is several facts: read .known, .iface, .stale, "
                        ".unreadable and .invalid")

    def __getitem__(self, field):
        raise TypeError(f"ownership is a decoded record, not the scalars it came from; {field!r} is "
                        "not reachable and re-deriving it from them is the defect")

    @property
    def known(self) -> bool:
        """Did every one of the four records answer, and answer in full?"""
        return not self.unreadable

    def iface_or(self, fallback: str) -> str:
        """The interface the panel's addresses are recorded ON, or `fallback` when it claims NONE.

        THE ONE PLACE BLANK AND INVALID PART WAYS, and the reason this is a method rather than
        `own.iface or plan.segment_iface` at each call site. Blank is a first pass on a fresh host:
        there is no recorded interface, the plan's is the only candidate, and substituting it is how
        an address left by an earlier release still gets retired. A value that is not an interface
        name is not that at all — it says the panel's addresses are on SOMETHING, and the one thing
        that cannot be concluded is that they are on the interface the plan happens to name. Spending
        the fallback there aims `ip addr del <recorded address>` at the interface the pass is
        installing onto, which is the live segment.

        So an unusable value answers "" and every pair derived from it is not formed: nothing is
        compared, nothing is persisted, nothing is deleted, and the value travels to the operator
        through `invalid` instead.
        """
        return "" if self.iface_unusable else (self.iface or fallback)


def _ledger_entries(pairs) -> tuple[list[str], list[str]]:
    """The stale ledger's text for `pairs`, and a reason per pair that would NOT READ BACK as one.

    THE WRITE SIDE OF `_read_ownership`, and what makes the ledger that used to wedge the gateway
    unreachable instead of tolerated. The ledger is one `<iface> <addr>` per line and is parsed by
    splitting on whitespace, so a value carrying a space of its own — `eth0.2 eth0.9` as the
    interface, the shape a hand-edited database or a truncated write produces — serialises to THREE
    tokens. Every later read of the record then refuses it in full, correctly, and both paths that
    read it are the paths that install and remove the segment's addresses: the segment could not be
    provisioned or cleared again until someone edited the database by hand.

    Reached only because the pass derived a pair from an unvalidated scalar; the boundary no longer
    hands one over. This is the backstop that makes that structural rather than incidental — the
    entry is checked against the parse that will read it, and a ledger that would not survive the
    round trip is NOT WRITTEN AT ALL. Nothing is coerced on the way (`f"{iface} {addr}"` on a value
    of the wrong type is exactly the coercion this module refuses everywhere else), and a pair with
    a blank half is not a pair.
    """
    entries: list[str] = []
    refused: list[str] = []
    for iface, addr in pairs:
        if not isinstance(iface, str) or not isinstance(addr, str):
            refused.append(
                f"{STALE_KEY} was not written: {(iface, addr)!r} is not an interface and an address "
                "as text, so what the ledger would hold cannot be read back")
            continue
        entry = f"{iface} {addr}"
        if entry.split() != [iface, addr]:
            refused.append(
                f"{STALE_KEY} was not written: {entry[:80]!r} is not one interface and one address, "
                "so the ledger would not read back as one the panel wrote and every later pass "
                "would refuse to install or remove a segment address")
        elif entry not in entries:
            entries.append(entry)
    return entries, refused


def _record_ownership(store, iface: str, addr4: str, addr6: str,
                      stale: list[tuple[str, str]], retained=()) -> list[str]:
    """Record what the panel owns on the segment, and PROVE every part of it. A reason per part that
    could not be proven; empty when the record now says all four things.

    FOUR RECORDS THAT ONLY MEAN ANYTHING TOGETHER, which is why a partial write is its own failure
    and not a lesser version of a total one. Written unverified, a `set_setting` that kept nothing
    could leave `managed_segment_iface` naming the OLD interface while `_addr4` names the new
    address — so the next removal is aimed at an interface that never had it, and the interface that
    does keeps an address the panel no longer records — or drop the stale ledger, which is a retry
    list, leaving an address on the host that nothing will ever come back for. Both are the exact
    orphan every other guarantee in this module is arranged around, reached through the record
    rather than through the kernel.

    SO THE GROUP HAS ONE WRITE-AHEAD STEP, and it is the STALE LEDGER, proven before any scalar
    moves. Verifying the four separately is not enough, because the scalars and the ledger do not
    hold the same news: the scalars say where the segment is going, the ledger is the only record of
    the pair it is coming OFF. Written in the other order, a store that took the scalars and then
    refused the ledger's journal left `managed_segment_iface` naming the candidate with the old live
    pair recorded NOWHERE — a snapshot that is internally consistent, reads as known, and has just
    dropped the interface that is still up carrying the segment out of the cover, out of the NM
    drop-in and off every retry list. Reporting failure afterwards does not give it back.

    This is the discipline `write_set` already applies to one record, lifted to the group: the thing
    that must survive is written and PROVEN first, and until it is, nothing has changed at all. On
    that failure the function returns immediately — the scalars still name the old live pair, so the
    next pass reads the host correctly and retries the whole step — and `write_set`'s own journal
    holds underneath, so a ledger the store half-replaced still reads complete. Every remaining
    failure is a scalar, and what it leaves behind is the over-recording direction: a pair named in
    both the ledger and the current-address keys costs a retried delete, not a lost address.

    `retained` is the ledger's own unreadable-but-parseable entries, handed back by the boundary and
    written through VERBATIM (see `Ownership`): the rewrite may not drop the only record of an address
    that may be on the host, and it may not rebuild one out of parts it could not read either.

    AND THE LEDGER IS REFUSED IF IT WOULD NOT READ BACK. Checked before the write-ahead step, so
    nothing has changed when it fires, and it is what keeps a value carrying a space from becoming a
    three-token entry that blocks provisioning for good (see `_ledger_entries`).
    """
    entries, refused = _ledger_entries(list(stale) + list(retained))
    if refused:
        _log.error("the ledger of panel-owned segment addresses was not written: %s",
                   "; ".join(refused))
        return refused
    why = write_set(store, STALE_KEY, entries)
    if why:
        return [why]
    reasons: list[str] = []
    for key, value in zip(OWNERSHIP_KEYS, (iface, addr4, addr6)):
        why = write_record(store, key, value)
        if why:
            reasons.append(why)
    return reasons


def _read_ownership(store) -> Ownership:
    """THE BOUNDARY: the four ownership records, decoded once, as an `Ownership`. Never raises.

    A caller whose record is not `known` has no trustworthy picture of what the panel owns and must
    not touch addresses on that basis: the stale ledger it would write is one the unread entries have
    already dropped out of. That is unchanged, and so is which shapes reach it — a store that will
    not answer, and a set record that cannot be read IN FULL, because the rewrite is computed from
    what parsed and an entry that did not is an address on the host whose last record this pass would
    drop.

    WHAT IS NO LONGER THERE is a scalar the store ANSWERED with and that is merely not text. The three
    scalars are read through `_read_scalar`, which keeps a store fault apart from returned-invalid data,
    so a `managed_segment_iface` holding a number no longer makes this whole record unreadable — which
    made `_ownership_cover` raise and the pass decline above the only step that rewrites the scalar, on
    every pass, until someone edited the database. It is `invalid` instead: no name, no pair, no
    comparison, no delete, reported, and rewritten on the way past. The LEDGER is still read through
    `read_record`, where the collapse is correct: its entries are kept verbatim, so the panel cannot
    heal one and the two failures cost the same.

    WHAT IS NEW IS EVERYTHING BELOW THAT LINE. Both scalar addresses are canonicalised into the one
    spelling the kernel prints and checked against the family their key names, and every ledger pair
    has its interface proven a name and its address canonicalised, BEFORE any of it is compared,
    persisted or deleted. Read as raw text, an expanded or upper-case IPv6 semantically equal to the
    address the pass is installing was not equal to it: it was classified as superseded, it escaped
    the desired-address protection — a text comparison too — and it was deleted off the interface
    moments after its canonical spelling was installed there. That is the segment's own address, on a
    live gateway, and it is the whole reason both sides of every comparison here are now one spelling.

    AND THE INTERFACE'S SHAPE IS PROVEN, which a previous round deliberately declined to do. Its
    reasoning was that refusing would cost the segment its addresses — this read gates installing
    them — and that a retained value carrying a space would make the ledger unparseable for ever. Both
    were true of "validate and refuse" and of "validate and retain"; neither is true of what happens
    now. An invalid value is not an answer AND not a refusal: `iface_or` hands back "" instead of
    substituting the plan's interface, so no pair is formed from it, nothing is compared with it,
    nothing is deleted on its authority and nothing derived from it is persisted — while the pass
    carries on, installs the operator's addresses, rewrites the scalar and heals. The value travels to
    the operator through `invalid` (see `_invalid_ownership_reasons`), and the write side refuses a
    ledger it could not read back, so the unparseable state is unreachable rather than tolerated
    (`_ledger_entries`).

    A ledger pair in that state is RETAINED VERBATIM rather than dropped or rebuilt: it may be the
    only record of an address on the host, and an entry that parses as a pair goes back as one.
    """
    scalars = [_read_scalar(store, key) for key in OWNERSHIP_KEYS]
    stale, why = _read_pairs(store, STALE_KEY)
    unreadable = [record.reason for record, returned in scalars
                  if not record.known and not returned]
    if stale is None:
        unreadable.append(why)
    if unreadable:
        return Ownership(unreadable=tuple(unreadable))

    # A scalar the store ANSWERED with and that is not text is `invalid`, not `unreadable`, for the
    # same reason a value of the wrong shape is: the pass rewrites all three scalars, so refusing over
    # one refuses the repair too — for ever, since nothing above the reconcile ever writes them (see
    # `_read_scalar`). It authorises exactly nothing on the way past: no name, no pair, no comparison.
    invalid: list[str] = [returned for _, returned in scalars if returned]
    iface, iface_unusable = "", bool(scalars[0][1])
    if not iface_unusable:
        try:
            named = _checked_names([scalars[0][0].text])
            iface = named[0] if named else ""
        except RecordUnknown as exc:
            invalid.append(str(exc))
            iface_unusable = True

    addrs: list[str] = []
    for (record, returned), key, family in zip(scalars[1:], OWNERSHIP_KEYS[1:], (4, 6)):
        if returned:
            addrs.append("")            # not an address: nothing to compare, nothing owed a removal
            continue
        try:
            addrs.append(_checked_family_addr(record.text, family, key) if record.text else "")
        except RecordUnknown as exc:
            invalid.append(str(exc))
            addrs.append("")

    kept: list[tuple[str, str]] = []
    retained: list[tuple[str, str]] = []
    for entry_iface, addr in stale:
        try:
            named = _checked_names([entry_iface])
            if not named:                   # a pair's halves are non-blank; belt and braces
                raise RecordUnknown(f"{STALE_KEY} holds an entry naming no interface at all")
            kept.append((named[0],
                         _checked_addr_text(addr, f"the panel-owned address on {entry_iface}")))
        except RecordUnknown:
            # Kept, and NOT reported from here. This is the one thing the panel cannot heal — the entry
            # goes back exactly as it came, so a pass that failed on it would fail every later pass
            # too, which is the refusal-for-ever this design rules out — so it travels to the operator
            # as a log rather than through the pass result. But this boundary is read about five times
            # in a normal pass, and it was logging every entry on each read: five identical ERRORs for
            # one durable fact. The decode is silent and the pass says it ONCE, at its own boundary
            # (see `_report_retained_ownership`).
            retained.append((entry_iface, addr))

    return Ownership(iface=iface, addr4=addrs[0], addr6=addrs[1],
                     stale=tuple(kept), retained=tuple(retained),
                     invalid=tuple(invalid), iface_unusable=iface_unusable)


def _invalid_ownership_reasons(own: Ownership) -> list[str]:
    """The ownership SCALARS that hold something the panel did not write, phrased for the address
    channel that carries them. Never the retained ledger entries — see `Ownership`.

    REPORTING IS THE WHOLE ACTION HERE. Nothing was compared with these values, nothing was derived
    from them and no address was removed on their authority; the pass applied the operator's change
    and rewrote all three scalars on its way past, so the record is CLEAN afterwards. That is what
    makes the ordinary channel — `_provision_result` -> `ok=False` -> `/api/ready`'s `provisioning`
    check — the right one rather than a wedge: it costs one refused change, and the heal that comes
    with it means the retry does not hit the same value. Silently self-healing is what let a value
    like this sit in the database unnoticed until a refused replacement turned it into a ledger entry
    nothing could read.

    Returned as a list so a caller can concatenate it without testing anything, which is what keeps
    this from growing a branch (the same shape as `_backlog_warning`).
    """
    if not own.invalid:
        return []
    reason = ("the panel's record of what it owns on the segment holds a value it did not write, so "
              "nothing was compared with it, nothing was derived from it and no address was removed "
              "on its authority; the record has been rewritten, and what the old value referred to "
              "is still on the host and has to be checked there (ip addr show) and cleared by hand: "
              + "; ".join(own.invalid))
    _log.error("%s", reason)
    return [reason]


def _report_retained_ownership(store) -> None:
    """Name the ledger entries the panel is KEEPING VERBATIM — once, at the pass boundary. Never
    raises, and changes nothing.

    THE ONE PLACE THEY ARE REPORTED. A retained entry is the one state here the panel cannot heal: it
    goes back exactly as it came on every pass, so it cannot travel the pass's failure channel (that
    would refuse the operator's change for ever) and it cannot be repaired into silence either. What is
    left is telling the operator, and the boundary that decodes it is the wrong place to do that from
    — `_read_ownership` is read about five times in one pass (the cover before the reconcile, the
    reconcile, the cover after it, the drop-in decision, the narrowing decision), and it logged every
    retained entry on each of them. Five identical ERRORs per normal pass, for a fact that has not
    changed, which is how a durable one-line problem comes to look like a storm.

    So the decode is silent and the PASS says it, exactly once, here. Read-only: the entry is untouched
    and stays exactly where it is, which is the whole point of retaining it.
    """
    own = _read_ownership(store)
    if not own.retained:
        return
    _log.error("the ledger of panel-owned segment addresses holds %d entr%s the panel did not write; "
               "they are kept exactly as they are and nothing is compared with them, removed on their "
               "authority or rebuilt from them, so they have to be checked on the host (ip addr show) "
               "and cleared by hand: %s",
               len(own.retained), "y" if len(own.retained) == 1 else "ies",
               "; ".join(f"{iface} {addr}" for iface, addr in own.retained))


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
                  protected=frozenset()) -> tuple[list[tuple[str, str]], list[str]]:
    """Remove each panel-owned address in turn; return `(pairs to keep recorded, reasons)`.

    A pair whose removal is not proven stays in the ledger, so the next pass retries it, and its
    reason travels back to the caller — which reports it through the pass result. Nothing is
    forgotten on a failure: that is how a refused delete used to become an address on the host
    with no record of it anywhere.

    This is the module's only address-deletion site, which is why `protected` is enforced here too
    and not only where the ledger is written (see `_desired_pairs`): a pair the segment is required
    to have is skipped, whatever the ledger or the caller says. That is the pair the pass installs
    on the path that applies a plan. The clear path passes none, correctly — it runs only with
    segment management off, where the panel desires no address at all.
    """
    keep: list[tuple[str, str]] = []
    reasons: list[str] = []
    for iface, addr in _distinct(pairs, protected):
        reason = _retire_owned_addr(iface, addr, run)
        if reason is not None:
            _log.warning("could not remove the panel-owned address %s", reason)
            keep.append((iface, addr))
            reasons.append(reason)
    return keep, reasons


def _backlog_warning(kept: list[tuple[str, str]]) -> list[str]:
    """A reason naming an unusually large stale ledger, or nothing at all. Never a decision.

    The one thing this does is TELL, and it is deliberately the weakest mechanism that still
    reaches a human: no branch of the pass reads it, no record is dropped for it, and no change is
    declined because of it (see `BACKLOG_WARN` for why the cap it replaced was worse than the
    growth it bounded). It is appended to the reasons the pass already returns, so it travels the
    ordinary address-failure channel — `_provision_result` -> `ok=False` -> the caller's rollback
    and `/api/ready`'s `provisioning` check — alongside the individual failures that built it.

    Returned as a list so a caller can concatenate it without testing anything, which is what keeps
    it impossible for this to grow a branch later.
    """
    # `kept` is what the pass RETRIED and could not remove, which is deliberately not the whole
    # ledger: an entry the boundary could not read is preserved and never retried (see `Ownership`),
    # so counting it here would both misdescribe this warning — "every one of them was retried on
    # this pass" — and give an unhealable record a way to fail the pass once enough of them piled up.
    if len(kept) < BACKLOG_WARN:
        return []
    reason = (f"{len(kept)} panel-owned segment addresses are awaiting removal, far more than a "
              f"rotating segment produces ({BACKLOG_WARN} is already four rotations' worth); every "
              "one of them was retried on this pass and would not go, and the panel will keep "
              "retrying them and forget none, but a backlog that does not drain by itself has to "
              "be cleared on the host by hand (ip addr del <address> dev <interface>) — the "
              "individual removal failures are reported alongside this")
    _log.error("the panel-owned address backlog is not draining: %s", reason)
    return [reason]


@dataclass(frozen=True)
class AddressOutcome:
    """What a segment-address pass did, which is TWO facts and cannot be one.

    `applied` — the plan's addresses are on the interface now. `reasons` — what the pass could not
    settle, whether or not it applied anything.

    They were one list of reasons, and that is the defect this type exists to end: "installed, but
    a superseded address would not go" and "not installed, so the interface still has the OLD
    address" both came back as a non-empty `list[str]`, indistinguishable. Every caller read a list
    as a warning and carried on to configure what depends on the plan — dnsmasq above all, whose
    DHCP range, router option and listen address are all derived from `plan.segment_ip` — against
    an address the interface does not have. A segment served DHCP for a subnet that was not on it.

    So the two facts are separate fields, and a caller must name `applied` before it configures
    anything keyed to the plan. `reasons` cannot be reached without naming it either, and the
    object deliberately refuses to be a truth value at all (see `__bool__`), so the older `if
    reasons:` habit fails loudly instead of reading "not installed" as "applied with warnings".

    `applied` is False for exactly one reason now: the kernel would not take the addresses. It used
    to be False for a second one — a change the ownership ceiling declined — and that ceiling is
    gone, so nothing here ever means "the panel chose not to try".
    """
    applied: bool
    reasons: list[str] = field(default_factory=list)

    def __bool__(self):
        """Never a truth value: the two outcomes it distinguishes are both truthy and both falsey.

        A pass that applied the plan and could not retire one superseded address is a success with
        a warning; a pass whose addresses never landed is a failure. Under `if outcome:` they are
        identical, which is the confusion this type replaced, so asking is an error, not a guess.
        """
        raise TypeError("an address outcome is two facts: read .applied and .reasons")


def reconcile_segment_addresses(store, plan: NetPlan, run=_run) -> AddressOutcome:
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

    No size of backlog makes this pass decline the operator's change: the plan is applied, every
    recorded pair is retried, and an unusually large ledger is REPORTED rather than enforced (see
    `BACKLOG_WARN`). The refusal that used to live here had to be ordered correctly against every
    other step of a host reconfiguration, and twice was not — a retarget could lose the old link and
    address and then decline to install the new one.

    Returns an `AddressOutcome`: whether the plan's addresses are ON the interface, and a reason per
    thing the pass could not settle. The two are separate because they are separate facts, and a
    caller must not configure anything keyed to this plan when nothing was applied.

    EVERY COMPARISON BELOW IS BETWEEN TWO CANONICAL SPELLINGS. The record's side is canonicalised at
    the boundary (`_read_ownership`) and the plan's here (`_canonical_addr`), because a record may
    hold any spelling of an address and the pass decides what is superseded by asking whether it
    differs from the one being installed. Compared as text, `FD00:1:2:3:0:0:0:1/64` was different from
    `fd00:1:2:3::1/64`, so it was appended to the stale ledger, was not recognised by the
    desired-address protection either, and was deleted off the interface immediately after the same
    address was installed there.
    """
    own = _read_ownership(store)
    if not own.known:
        # NOTHING IS INSTALLED ON A RECORD THAT CANNOT BE READ. The ledger written below is computed
        # from the one read here, so an unreadable read would silently drop every entry it holds —
        # the addresses would go on and the record of what is owed a removal would be gone. Failing
        # here costs the pass; the caller rolls back and reports, and the host is untouched.
        reason = ("the panel's record of what it owns on the segment could not be read, so no "
                  "address was installed: " + "; ".join(own.unreadable))
        _log.error("%s", reason)
        return AddressOutcome(False, [reason])
    # A value the panel did not write blocks only the action it would have authorised: no pair is
    # formed from it, so nothing is compared with it and nothing is deleted on its authority, and it
    # travels to the operator through the reasons this pass already returns. The operator's change
    # still goes on — refusing it here is the outage, not the fix (see `_read_ownership`).
    reported = _invalid_ownership_reasons(own)
    new_iface = plan.segment_iface
    new4 = _canonical_addr(f"{plan.segment_ip}/24")
    new6 = host_addr6(plan.segment_ip6) if plan.ipv6_enabled else None
    new6 = _canonical_addr(new6) if new6 else None
    # Blank means "nothing recorded yet", and the plan's interface is then the only candidate the
    # recorded addresses could be on. An UNUSABLE value means the opposite — they are on something,
    # and the one thing that cannot be concluded is that it is the interface being installed onto —
    # so it answers "", and the two superseded pairs below are simply not formed (`iface_or`).
    old_iface = own.iface_or(new_iface)

    desired = _desired_pairs(new_iface, new4, new6 or "")
    recorded = _distinct(own.stale, desired)
    superseded = []
    if old_iface and own.addr4 and (own.addr4 != new4 or old_iface != new_iface):
        superseded.append((old_iface, own.addr4))
    if old_iface and own.addr6 and (own.addr6 != (new6 or "") or old_iface != new_iface):
        superseded.append((old_iface, own.addr6))
    rotating = _distinct(superseded, desired | frozenset(recorded))
    stale = recorded + rotating

    # ...AND NOTHING IS INSTALLED ON A RECORD THAT CANNOT BE WRITTEN EITHER. This is the record the
    # whole ordering rests on — written before the kernel so a failure, or a caller whose transaction
    # rolls back mid-apply, still finds every address to remove — and a `set_setting` that quietly
    # keeps nothing makes that promise silently false. The addresses would go on with the ownership
    # of them recorded nowhere: the orphan no later pass looks at.
    #
    # `stale` is the pass's write-ahead set and holds every pair it is superseding, so the group
    # writes it FIRST and proves it: until it is on disk nothing may name the candidate, or a store
    # that took the scalars and refused the ledger would leave the old live pair recorded nowhere at
    # all (see `_record_ownership`).
    unwritten = _record_ownership(store, new_iface, new4, new6 or "", stale, own.retained)
    if unwritten:
        reason = ("the panel could not record what it was about to install on the segment, so it "
                  "installed nothing: " + "; ".join(unwritten))
        _log.error("%s", reason)
        return AddressOutcome(False, reported + [reason])

    # Each command is carried alongside THE ADDRESS IT INSTALLS, because the reason has to name
    # the one that was actually rejected. Both used to run under one `try` that blamed `new4`
    # whatever failed, so a refused IPv6 replacement reported the IPv4 address — which is on the
    # interface and working — as the failure, and never printed the IPv6 target at all. That is
    # precisely backwards for the case it matters most in: a delegated prefix the kernel will not
    # take is diagnosed by the address the kernel named, and the operator was shown another one.
    for addr, cmd in [(new4, ["ip", "addr", "replace", new4, "dev", new_iface])] + (
            [(new6, ["ip", "-6", "addr", "replace", new6, "dev", new_iface])] if new6 else []):
        try:
            run(cmd)
        except subprocess.CalledProcessError as exc:
            # The only remaining way the plan's addresses are not on the interface: the kernel
            # would not take them. Nothing is retired here — the interface may still be carrying
            # the address this pass meant to supersede, and deleting it after a failed install is
            # how a half-applied pass becomes a segment with no address at all. The record written
            # above stays as it is, a superset of everything either address may now be, so the next
            # pass finds all of it. The same holds for the link: `applied=False` is what stops the
            # caller retiring the VLAN this one supersedes (see `retire_superseded_links`).
            # `OSError` is deliberately not caught, exactly as in `_retire_owned_addr`: no `ip`
            # binary at all is not one address failing, and travels out to the caller's own handler.
            why = ((exc.stderr or "").strip() or str(exc)
                   or f"ip addr replace exited {exc.returncode}")
            _log.error("the segment address %s could not be installed on %s: %s",
                       addr, new_iface, why)
            return AddressOutcome(False, reported + [f"{addr} on {new_iface}: {why}"])

    keep, reasons = _retire_owned(stale, run, desired)
    # The addresses ARE on the interface now, so this shrink cannot un-apply the pass; an unprovable
    # write here leaves the ledger naming pairs already removed, which only costs a retried delete
    # and an over-covered interface. It is still a record that does not describe the host, so it
    # travels the reasons channel and fails the pass.
    reasons += _ownership_reasons(
        _record_ownership(store, new_iface, new4, new6 or "", keep, own.retained))
    return AddressOutcome(True, reported + reasons + _backlog_warning(keep))


def _ownership_reasons(unwritten: list[str]) -> list[str]:
    """Record-write failures phrased for the address channel that carries them."""
    return ([("the ledger of panel-owned segment addresses could not be updated, so a later pass "
              "will retry what it names: " + "; ".join(unwritten))] if unwritten else [])


def clear_managed_addresses(store, run=_run) -> list[str]:
    """Remove only addresses previously installed by this panel and clear ownership state.

    Returns a reason per address whose removal is not proven. Each of those stays on the stale
    ledger — the current-address keys are cleared, because the panel no longer claims them as the
    segment's, but the pair is still recorded as owed a removal, and this same path runs on every
    later pass while segment management is off, so it retries. Reporting matters more here than
    anywhere else: with management off, readiness skips the segment-address check entirely, so the
    pass result is the only place a leftover the panel owns can be seen.

    Which is also why the backlog warning is raised from here and not only from the reconcile path:
    this path moves the current pairs ONTO the backlog and blanks the current-address keys, so a
    host that refuses every removal reaches its largest ledger here, in the one mode where nothing
    else is looking (see `BACKLOG_WARN`).

    A recorded value the panel did not write is reported and acted on in no other way, exactly as on
    the reconcile path: with no usable interface there is no pair to aim a delete at, so the current
    keys are still blanked — the panel no longer claims that segment — and what the value referred to
    is left where it is, named to the operator (see `_invalid_ownership_reasons`).
    """
    own = _read_ownership(store)
    if not own.known:
        # Same rule as the reconcile path, and it matters more here: this path is the only thing that
        # ever retires these pairs, so a ledger rewritten from a read that failed strands them for
        # good — with segment management off, invisible to readiness too.
        return ["the panel's record of what it owns on the segment could not be read, so nothing "
                "was removed: " + "; ".join(own.unreadable)]
    reported = _invalid_ownership_reasons(own)
    # Written explicitly rather than leaning on `_distinct` dropping a blank half: with no usable
    # interface there is no pair, and "there is no pair" is the decision, not a side effect.
    current = [(own.iface, own.addr4), (own.iface, own.addr6)] if own.iface else []
    keep, reasons = _retire_owned(list(own.stale) + current, run)
    reasons += _ownership_reasons(_record_ownership(store, "", "", "", keep, own.retained))
    return reported + reasons + _backlog_warning(keep)


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


def ensure_nm_unmanaged(seg: str, run=_run, write_file=None, nm_active=None,
                        also=()) -> None:
    """Tell NetworkManager to leave the segment alone (so it doesn't fight our addressing).
    Writes the drop-in unconditionally (honored whenever NM (re)starts); reloads NM live via
    nsenter into pid 1 only when NM is actually running.

    `also` names the interfaces the segment is being moved OFF, which stay unmanaged for the
    length of the move: the old link is still carrying the segment until it is retired, and
    handing it back to NetworkManager mid-move invites NM to reconfigure an interface that is
    still in service. They are dropped from the drop-in by the narrowing write at the end of the
    pass, so the steady state is one interface, exactly as it always was.
    """
    devices: list[str] = []
    for name in [seg, *also]:
        if name and name not in devices:
            devices.append(name)
    unmanaged = ";".join(f"interface-name:{name}" for name in devices)
    # Resolved at call time, exactly as `remove_nm_unmanaged` resolves its own seam: a default
    # bound at definition time is one a caller cannot replace.
    (write_file or _write_file)(NM_CONF_PATH, f"[keyfile]\nunmanaged-devices={unmanaged}\n")
    _nm_reload(run, nm_active)


def remove_nm_unmanaged(run=_run, remove_file=None, nm_active=None) -> None:
    """Hand the segment back to NetworkManager when the panel stops managing it.

    Without this the drop-in outlives the panel's ownership and NM refuses to manage the
    interface forever, so an operator who turns `manage_segment` off is left with a segment
    nobody configures.
    """
    (remove_file or _remove_file)(NM_CONF_PATH)
    _nm_reload(run, nm_active)


def _record_generated_prefix(store, key: str, prefix: str, what: str) -> str:
    """Persist a prefix this module just invented, PROVEN. Returns it, or raises.

    A generated prefix is only stable because it is written down: it is derived from fresh random
    bytes, so a `set_setting` that quietly kept nothing means the NEXT pass generates a DIFFERENT
    /64, installs it, supersedes the one before, and does it again — the segment renumbers on every
    pass and every client's v6 address moves under it. Returning a prefix that is not recorded is
    therefore worse than failing the pass, which is what the raise does: `host_provision` reports it
    and the addresses are left alone.
    """
    why = write_record(store, key, prefix)
    if why:
        raise RuntimeError(f"the {what} could not be recorded, so it was not used — a prefix that "
                           f"is not persisted is a different prefix on the next pass: {why}")
    return prefix


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
    _record_generated_prefix(store, "segment_ip6", ula, "generated segment IPv6 prefix")
    _log.info("generated stable ULA prefix for the segment: %s", ula)
    return ula


def effective_segment_prefix6(store, settings, rand=secrets.token_bytes, delegated=None) -> str:
    """Return the /64 to install without mutating the configured ``auto`` intent.

    Auto mode prefers a currently delegated /64 and otherwise uses a persistent ULA fallback,
    so client IPv6 remains deterministic while the upstream PD lease is absent or renewing.

    `delegated` names the /64 to treat as the current delegation instead of reading the recorded
    one. That is what lets the PD watcher resolve a NEW delegation into a plan without persisting
    it first: a prefix written before the reconcile that installs it is a prefix a failure — or an
    exception on any line between — leaves recorded over a segment the host does not have. `None`
    means "read the record", which is every other caller (see `_pd_callback`).
    """
    if (store.get_setting("ipv6_enabled") or "0") != "1":
        return ""
    intent = (store.get_setting("segment_ip6") or settings.segment_ip6 or "").strip()
    if intent.lower() != "auto":
        return ensure_segment_prefix6(store, settings, rand=rand)
    if delegated is None:
        # Typed, because this is the one read whose failure would be spent as a DIFFERENT prefix:
        # falling through to the ULA fallback on an unreadable delegation installs a /64 the
        # delegation was meant to replace, and supersedes the one that is on the interface.
        record = read_record(store, "pd_segment_prefix6")
        if not record.known:
            raise RecordUnknown("the recorded delegated IPv6 prefix could not be read, so the "
                                "segment prefix could not be resolved: " + record.reason)
        delegated = record.text
    delegated = delegated.strip()
    if host_addr6(delegated):
        return delegated
    ula = (store.get_setting("ula_prefix6") or "").strip()
    if not host_addr6(ula):
        _, vid = parse_vlan(store.get_setting("segment_iface") or settings.segment_iface)
        ula = generate_ula_prefix(vid if vid is not None else 0, rand=rand)
        _record_generated_prefix(store, "ula_prefix6", ula, "generated ULA fallback prefix")
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


def _read_managed_state(store) -> tuple[list[str], str, str, str]:
    """`(unreadable, iface, addr4, addr6)` — the ownership keys, typed. Never raises.

    THE INTERFACE IS CHECKED FOR ITS SHAPE, not merely for being text, and that check is what makes
    the value usable by the one thing the undo does with it. This is the panel's record of the
    segment it went BACK to, and the undo compares it with the candidate to answer "is the candidate
    something other than the interface now in service". A value richer than one name —
    `"eth0.9 eth0.2"`, the shape a hand-edited database or a truncated write produces — is not equal
    to `eth0.9`, so that conjunct PASSED on it, the otherwise valid ownership checks passed too, and
    the undo issued `ip link delete eth0.9` against the interface the operator's segment was on. An
    inequality against a value whose shape cannot be confirmed is not evidence of anything.

    So the value goes through the gate every cover source goes through (`_checked_names`, which is
    `render._IFACE_RE` and not a second copy of it), and one that is not a single interface name is
    UNREADABLE — the same answer, and the same cost, as a store that would not answer at all:
    nothing is removed, nothing is recorded, the pending record stays, and the enforcement keeps
    covering the candidate from it. What is refused is the undo's one ACTION, not the pass and not
    the gateway (see `undo_provision_candidate`).

    A non-text value never reaches the check: `read_record` already refuses to coerce one, so it
    arrives here as unreadable for that reason instead. The addresses are deliberately NOT
    canonicalised here — they gate nothing, they are compared in both spellings where they are used,
    and a malformed one can only add an orphan to the report (see `_orphan_pairs`).
    """
    records = [read_record(store, key) for key in OWNERSHIP_KEYS]
    unreadable = [record.reason for record in records if not record.known]
    if unreadable:
        return unreadable, "", "", ""
    try:
        named = _checked_names([records[0].text])
    except RecordUnknown as exc:
        return [str(exc)], "", "", ""
    return [], (named[0] if named else ""), records[1].text, records[2].text


def managed_host_state(store) -> dict:
    """The interface and addresses the panel currently claims ownership of.

    The two in-transaction callers read this to learn what a pass CLAIMED, and are already inside a
    handler for a store that will not answer, so this one keeps the raising contract it always had.
    The undo reads `_read_managed_state` instead, because there an unanswerable read decides whether
    a link may be deleted (see `undo_provision_candidate`).

    NOR IS THE SHAPE PROVEN HERE, and it does not have to be: the dict goes to exactly one consumer,
    as the undo's `installed`, and that consumer decodes it through `_as_candidate` before a field of
    it is used — so a value that is not one interface name, or not one canonical address, makes the
    whole undo unreadable there rather than being acted on here.

    WHICH ONLY HOLDS IF THE VALUE ARRIVES THERE, and `or ""` is why it did not. It ran BEFORE the type
    gate and flattened every FALSEY non-text value — `0`, `False`, `[]`, `{}` — into the one string
    that means "the panel claims no address here". So the gate the paragraph above relies on was never
    reached: the undo read a legitimate absence, left the address that IS installed out of the orphan
    report and out of the survivor ledger, and settled the pending record over a record it had not
    read. `None` is the store's own "no such key" and is the only value mapped, exactly as
    `read_record` maps it; everything else is handed over UNCHANGED, so the decode sees what the store
    holds and refuses it — the undo stays pending and the enforcement keeps covering the candidate.
    """
    raw = [store.get_setting(key) for key in OWNERSHIP_KEYS]
    return dict(zip(("iface", "addr4", "addr6"),
                    ["" if value is None else value for value in raw]))


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
    """A settings FLAG as a yes or a no, and the one place text is still made of a stored value.

    Deliberate, and safe for the reason nothing else on this path is: the answer is a bool, not a name
    or an address, and every shape but the handful of yeses below reads as NO. So a malformed
    `manage_segment` lands where `host_provision`'s own gate lands it — `!= "1"`, the clear path — and
    the two readers agree that the pass installs nothing and records no candidate. It authorises
    nothing, names nothing, and is never interpolated into a command (see `_checked_text` for the
    fields where a coercion did all three).
    """
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
    must fail before it installs anything.

    Which is why the write is PROVEN and not merely attempted. "Unguarded" only ever covered the
    store that raises; one that returns having kept nothing left the caller believing it had a
    recovery record and about to provision a candidate interface nothing would reclaim — the same
    end state as no record at all, reached without an exception to stop it.

    And the SHAPE is proven before the write, by the same check the readers apply: a record they will
    refuse to act on is a record with no recovery story either, and the only moment that can still be
    reported to the operator instead of discovered on a later boot is before the pass runs. This
    cannot fail on anything `provision_candidate` builds; what it catches is a segment interface that
    is not an interface name arriving through a restore document, which the render would reject a
    moment later anyway — with the candidate already on the host. It is also where `resolved` is
    refused: the readers' shared check now knows that word belongs to the clear alone, so a live
    candidate carrying it cannot be written as an apparently successful pending record
    (`_refuse_resolved`).

    WHAT IS WRITTEN IS THE CANONICAL RECORD, in the addresses' case: the decode hands back the one
    spelling `ip addr show` prints, and storing that is what makes the value the drain compares and
    the value the record carries the same value (see `_checked_addr_text`). Every other key the caller
    put there is kept verbatim — a phase marker like `armed`, or a field from an older release — so
    this normalises the two fields it understands and invents nothing.
    """
    if not candidate:
        return
    try:
        proven = _checked_candidate(candidate)
    except RecordUnknown as exc:
        raise RuntimeError("the host-provisioning undo record would not be readable as one the panel "
                           "wrote, so the change was not applied: " + str(exc)) from exc
    stored = dict(candidate)
    for field_name, addr in (("addr4", proven.addr4), ("addr6", proven.addr6)):
        if field_name in stored:
            stored[field_name] = addr
    why = write_record(store, PROVISION_UNDO_KEY, json.dumps(stored))
    if why:
        raise RuntimeError("the host-provisioning undo record could not be written, so the change "
                           "was not applied: " + why)


# The TERMINAL form of the pending record: a settled undo, which every reader below ignores. It
# exists because blanking is the least trustworthy half of a clear — a store that quietly keeps
# nothing leaves the previous JSON in place, and that record means "this interface may still be
# live", so the ruleset would keep naming a candidate for ever and could capture unrelated traffic
# once the name is reused. So the resolution is written as its own value and PROVEN, and the blanking
# is best effort on top of it (see `clear_record`).
RESOLVED_UNDO = json.dumps({"resolved": True})

# `RESOLVED_UNDO` as it reads back, which is the ONLY record a reader may treat as settled.
_TERMINAL_UNDO = {"resolved": True}


def _settled_undo(candidate: dict) -> tuple[bool, str]:
    """Is this record the settled form — and if it only claims to be one, why it may not be read as
    one. `(True, "")`, `(False, "")` for an ordinary pending record, or `(False, why)`.

    THE EXACT SHAPE, by identity and by key set, never by truthiness. `resolved` used to be read as
    `if candidate.get("resolved")`, so `{"resolved": "false", "iface": "eth0.9"}` — a hand-edited
    database, a foreign backup document, a half-decoded write — was TERMINAL: a non-empty string is
    truthy, so a record still naming a candidate interface became a KNOWN-EMPTY cover, and the pass
    that read it concluded it had nothing left to cover and narrowed the ruleset off an interface that
    may have been up carrying the segment. Which is the bypass this record exists to prevent.

    Identity and not equality, because `1 == True` in Python: `{"resolved": 1}` compares equal to the
    terminal record and is not one the panel wrote. And the key set, because a record that says BOTH
    "settled" and "the candidate is eth0.9" is not settled on the strength of half of itself; the
    terminal form the clear writes carries `resolved` and nothing else.

    Everything else that mentions `resolved` is therefore INCONSISTENT, which here means unknown —
    covered, refusing, retained for repair — exactly as an interface name that cannot be read is
    (see `_pending_candidate`). Never terminal, and never a licence to act.
    """
    if candidate.get("resolved") is True and set(candidate) == set(_TERMINAL_UNDO):
        return True, ""
    if "resolved" not in candidate:
        return False, ""
    return False, ("the pending host-provisioning undo says it is settled and does not say it in the "
                   f"settled form ({json.dumps(candidate, default=repr)[:200]}), so whether it is "
                   "still naming a live candidate interface cannot be read")


def clear_provision_candidate(store) -> str:
    """Settle the pending record. "" or why it is still there. Never raises.

    Guarded, for the reason it always was: nothing here may turn a finished operation into a failure,
    and a record that survives only costs a redundant undo attempt later. What it may NOT do is
    assume: a non-raising `set_setting` was treated as proof of deletion, so a store that kept
    nothing left the pending record — and therefore the enforcement cover's fallback source — naming
    a candidate interface indefinitely, long after the survivor ledger it was covering for had
    drained. Now the record is taken to its terminal form and read back, and the caller is told when
    even that could not be done.
    """
    why = clear_record(store, PROVISION_UNDO_KEY, terminal=RESOLVED_UNDO)
    if why:
        _log.error("the pending host-provisioning undo could not be settled, so the enforcement "
                   "keeps covering the candidate interface it names: %s", why)
    return why


@dataclass
class Pending:
    """What the pending undo record holds, in the answers its two readers need kept apart.

    `candidate` — a DECODED record naming an interface a pass may have put the segment on: the undo
    acts on it and the cover names it. It is a `Candidate` or `None`, never a dict and never an empty
    one, so a reader cannot reach a raw field and re-derive what the decode ruled out. `unusable` —
    why a value that IS there cannot be used, which is discarded once and reported. Neither, and no
    reason, means settled: a blank record or one already taken to its terminal form, which is not an
    error and not worth a word on every boot.

    `cover` is the same record as the enforcement sees it, and the one place where "the store could
    not answer" may not become "there is no candidate". Note that a record which cannot be READ is
    never `unusable`: it may be a perfectly good one naming a live interface, so nothing is discarded
    and the cover keeps naming what it cannot rule out. Neither is one whose `iface` is not an
    interface name, nor one whose other fields are not the shapes the panel writes — the same fact one
    level in, and discarding it would throw away exactly the coverage this record exists to provide.
    `unusable` is reached from ONE verdict only: a record that conforms and says there is no candidate
    (see `_pending_candidate`).
    """
    candidate: "Candidate | None" = None
    unusable: str = ""
    cover: "Cover" = field(default_factory=lambda: Cover())


def _pending_candidate(store) -> Pending:
    """Read the pending undo record once, for both of its readers. Never raises."""
    record = read_record(store, PROVISION_UNDO_KEY)
    if not record.known:
        return Pending(cover=Cover(unknown=["the pending host-provisioning undo could not be read: "
                                            + record.reason]))
    raw = record.text
    if not raw:
        return Pending()
    try:
        decoded = _decoded_record(raw)
    except RecordUnknown as exc:
        # A DUPLICATE KEY, which is a record that has already lost half of what it said before any
        # check could look at it — and possibly the half naming an interface that is up. So it lands
        # on the same side of the line as an `iface` nothing can read: unknown, refusing, RETAINED for
        # an operator to repair, and never `unusable`, because clearing it throws away the coverage it
        # stands for (see `_decoded_record`).
        return Pending(cover=Cover(unknown=[
            "the pending host-provisioning undo could not be decoded: " + str(exc)]))
    except (ValueError, TypeError) as exc:
        # Unusable AND unknown: it names no interface the cover could add, and until it is discarded
        # it may have been one that did, so it does not license a narrow either.
        return Pending(unusable=f"it could not be parsed ({exc}): {raw[:200]!r}",
                       cover=Cover(unknown=[
                           f"the pending host-provisioning undo could not be parsed: {exc}"]))
    if not isinstance(decoded, dict):
        return Pending(unusable=f"it is not a record: {raw[:200]!r}",
                       cover=Cover(unknown=["the pending host-provisioning undo is not a record"]))
    settled, inconsistent = _settled_undo(decoded)
    if settled:
        return Pending()                            # settled: the terminal form, and nothing to do
    # THE BOUNDARY. The record is decoded into the one type its readers use, checked for type, for
    # form and for the invariants that are about the whole record, and nothing below normalises
    # anything: the interface first, because a readable one is coverage whatever the rest of the
    # record says, then everything else (see `_checked_candidate_fields`).
    try:
        iface = _checked_candidate_iface(decoded)
    except RecordUnknown as exc:
        # THE LAST COVER SOURCE THAT WAS TAKING ITS NAME ON TRUST, and this record's whole reason for
        # existing is that it may be naming an interface that is UP carrying the segment. A value
        # like `eth0.2 eth0.9` — what a hand-edited database, a foreign backup document or a
        # truncated write produces — came back as ONE name, `known`: a rule naming it matches no
        # packet, while `eth0.2` and `eth0.9`, the interfaces it stood for, were named in neither the
        # kill-switch drop nor the tproxy redirect, and `may_narrow` let a pass conclude its cover was
        # complete. So it is validated through the same gate every other source goes through
        # (`_checked_names`, which is `render._IFACE_RE` and not a second copy of it), and the answer
        # is the one the read side already gives: no answer.
        #
        # AND IT IS NOT `unusable`, which is the half that matters here. `unusable` is discarded and
        # cleared by the boot resume, and clearing THIS record throws away the coverage it stands
        # for — the one interface a rolled-back pass may have put the segment on, in the window
        # before the survivor ledger can answer for it. A record whose interface cannot be read is
        # exactly as likely to be naming a live interface as one the store would not read at all, so
        # it is treated the same way: unknown, refusing, RETAINED, and left for the operator to
        # repair. No candidate is decoded, because there is nothing here an undo could safely act on.
        return Pending(cover=Cover(unknown=[
            "the pending host-provisioning undo names a candidate interface that cannot be read: "
            + str(exc)]))
    names = [iface] if iface else []
    if inconsistent:
        # A record that says it is settled and does not say it in the settled form. The name it also
        # carries is VALIDATED FIRST and kept as coverage, because that is the half a half-settled
        # record still makes a claim about: an interface a rolled-back pass may have put the segment
        # on. What it does not get is the other half — no candidate is decoded, so no undo acts on the
        # strength of a record nothing here wrote, and the record is not `unusable`, so the boot
        # resume keeps it rather than clearing away the coverage. Unknown, refusing, retained. Said in
        # its own words rather than as "the record the panel writes" (`_refuse_resolved` would refuse
        # it a line below) because this is the more specific fact about it.
        return Pending(cover=Cover(names=names, unknown=[inconsistent]))
    try:
        candidate = _checked_candidate_fields(decoded, iface)
    except RecordUnknown as exc:
        # The other half of the boundary, and the same answer: a record whose addresses, VLAN claim or
        # probe answer are not the shapes the panel writes is UNREADABLE, not partly readable. Its
        # interface still reads, so the cover keeps naming it — coverage is the half that is worth
        # keeping — while no candidate is decoded, so no undo acts on any of it: not the `vlan` claim
        # that authorises `ip link delete`, and not the addresses that would go into the survivor
        # ledger, where a bogus entry drains itself and takes the interface's coverage with it. Not
        # `unusable` either, for the reason above: retained, for an operator to repair.
        unreadable = (f"the pending host-provisioning undo names {iface or 'a candidate'} and is not "
                      f"the record the panel writes, so nothing may act on it: {exc}")
        return Pending(cover=Cover(names=names, unknown=[unreadable]))
    if not candidate.names_candidate:
        # A record that conforms IN FULL and says there is no candidate: no interface a pass could
        # have put the segment on, so there is nothing to cover and nothing a later boot could do
        # better with it. This is the ONE verdict that is `unusable`, and it is reached only from the
        # decoded record — asked of the raw dict it ran BEFORE the remaining fields were checked, so
        # `{"iface": "", "addr4": [1]}` was discarded as known-empty while not conforming at all.
        return Pending(unusable=f"it names no candidate interface: {raw[:200]!r}")
    return Pending(candidate=candidate, cover=Cover(names=names))


def pending_candidate_state(store) -> Cover:
    """`pending_candidate_ifaces` as two facts — see it, and `Cover`, for why the difference is the
    whole point of this record."""
    return _pending_candidate(store).cover


def pending_candidate_ifaces(store) -> list[str]:
    """The interface a PENDING undo record names — the enforcement cover's fallback source.

    The cover is normally derived from ledgers that are written when a decision is made and read on
    every later render. The surviving-candidate ledger is the one that answers for a rolled-back
    change, and there is a window in which it cannot: between the undo deciding to leave a candidate
    interface up and addressed, and the write that records it being PROVEN to have landed. A store
    that raises, or one that accepts the write and keeps nothing, never closes that window at all —
    and the recovery pass that renders the restored guard runs inside it (see the two rollbacks,
    which reconcile the previous host state BEFORE they reach the undo).

    This record is already on the host's disk when that window opens, written before the pass ran a
    single command precisely so a failure could still be recovered from, and it names the one
    interface the pass may have put the segment on. So while it exists, the cover names that
    interface too. That is a fallback and not a fourth ledger: it holds only until
    `remember_survivors` is proven to have recorded the pairs, at which point the undo has nothing
    unresolved and the caller clears this record on its own rule — and the ledger, which drains only
    on an explicit not-found from the host, becomes the single bookkeeping for that interface.

    Never raises: it is consulted by every store-derived enforcement render, which can only ever
    install the names it has.

    THE NAMES ONLY, and that is why this is not what the narrowing decision reads. A record that
    cannot be read or parsed, or whose `iface` is not an interface name, names no interface here —
    but it is not the same fact as a record that says
    there is no candidate, and swallowing the difference here is what let "unknown" be spent as
    "candidate absent": the cover came back empty, the pass concluded it had nothing left to cover,
    and the ruleset narrowed off an interface this very record existed to keep named. So the
    difference is kept in `pending_candidate_state`, and `Cover.may_narrow` is the only thing allowed
    to license the narrow.
    """
    return pending_candidate_state(store).names


def _merge(into: UndoOutcome, other: UndoOutcome) -> UndoOutcome:
    into.actions.extend(other.actions)
    into.unresolved.extend(other.unresolved)
    return into


def _vlan_claim(candidate: Candidate) -> tuple[bool, str]:
    """Does this record claim the panel created its interface as a VLAN — and if the record
    contradicts itself about it, why that is not an answer.

    `(True, "")` is the only value that lets the delete's remaining checks even be asked. `(False, "")`
    is a plain "no VLAN was created here": the key was absent, the answer was `null`, or it was
    `False` — a dotless segment interface, or the unarmed record a restore writes before it has
    touched the host. `(False, why)` is a record that disagrees with itself, which is reported and
    RETAINED.

    THIS IS AN AUTHORISATION, AND IT WAS TRUTHINESS. `candidate.get("vlan")` over a value out of JSON
    meant `"false"` — a non-empty string — authorised `ip link delete`, and the value sits next to an
    `iface` and a `link_state` in the same record: `{"iface": "eth0", "vlan": "false", "link_state":
    "absent"}` deletes eth0, the operator's own uplink, on the strength of a claim nothing here wrote.

    TWO OF THE THREE CHECKS THAT USED TO BE HERE ARE NOW THE TYPE'S. `candidate.vlan` is a real bool
    because only a real bool could decode into one, and `candidate.iface` is one interface name or ""
    for the same reason — a record spelling either of them any other way never becomes a `Candidate`
    at all, so it reaches no undo and is retained by its reader (see `_checked_candidate_fields`,
    `_pending_candidate`). What is left is the check that needs BOTH fields: the panel creates a VLAN
    and only a VLAN (`ensure_segment_link`), so `vlan: true` over a dotless name cannot have come from
    a pass here, and a record that contradicts itself authorises nothing.

    Anything refused is left on the host and reported. That direction costs one orphan the operator
    is told about; the other costs an interface the panel cannot put back.
    """
    if not candidate.vlan:
        return False, ""                        # no VLAN was created for this candidate
    if parse_vlan(candidate.iface)[1] is None:
        return False, (f"the candidate link {candidate.iface} was left on the host: the record says "
                       f"the panel created it as a VLAN and {candidate.iface!r} is not a VLAN "
                       "interface name, so the two halves of the record disagree")
    return True, ""


def _is_other_than_restored(iface: str, restored: dict) -> tuple[bool, str]:
    """Is `iface` PROVABLY not the interface the panel went back to? `(answer, why it cannot be
    asked)`.

    The conjunct that used to be written `iface != restored["iface"]`, and the reason it is a function
    now. A bare inequality answers YES for every value that is not a name — the one class of value it
    should never answer for at all — so `"eth0.9 eth0.2"` in the ownership record read as "the
    candidate is not the interface in service", and the delete's remaining checks, all of them
    legitimately true, took it from there to `ip link delete eth0.9`. The danger was never that the
    malformed value would be USED as a name; it was that it compared unequal to a real one.

    So the answer is only `True` when both sides are one interface name and the two names differ, and
    a restored value whose shape cannot be confirmed comes back as an unanswerable question, never as
    a difference. Blank is not that: an unrecorded scalar is a legitimate "the panel claims no segment
    interface" — what a host with segment management off records — and it differs from any candidate,
    exactly as it always did.

    `_read_managed_state` refuses such a value before an undo starts, so on the live path this cannot
    fire. It is checked here as well because THIS is where the value licenses a deletion, and the
    other rule this module keeps learning is that a precondition belongs at the site that acts on it:
    a later caller assembling `restored` from somewhere else inherits the guard instead of the defect.
    """
    try:
        names = _checked_names([restored.get("iface", "")])
    except RecordUnknown as exc:
        return False, str(exc)
    return iface != (names[0] if names else ""), ""


def _undo_candidate_link(state, candidate: Candidate, restored: dict,
                         run) -> tuple[UndoOutcome, bool]:
    """Remove the VLAN link a rolled-back pass created, or say why it was left alone.

    Returns what to report and whether the link was actually DELETED. Only a delete settles the
    addresses as well — a link that goes takes every address on it with it — so only a delete
    lets the caller stop there. A link that is KEPT, for any of the reasons below, may still be
    carrying an address the pass installed on it, an address that is now outside the restored
    ownership ledger and invisible to every later pass; returning early on that would leave the
    operator with no mention of exactly the orphan this ledger exists to surface.

    Everything below must be true before an `ip link delete` is issued: the record NAMES the link as
    an interface name, it claims a VLAN in the one form that is a claim, the name it claims that about
    is VLAN-shaped, the link is PROVABLY not the one the restored state is using, and the probe taken
    BEFORE the pass proved the link was not there (so the pass created it) while the one taken now
    proves it still is. A probe that cannot answer — then or now — is not one of them, and neither is
    a record whose halves disagree (see `_vlan_claim`).

    "PROVABLY not the one the restored state is using" is the fourth of those, and it used to be a
    bare `!=` against a value nothing had checked the shape of, which is the one comparison that
    answers "different" for a value that names no interface at all (see `_is_other_than_restored`).
    An unanswerable one is reported and RETAINED, like every other unreadable record here.

    THE FIRST TWO OF THOSE ARE NOW TRUE BY CONSTRUCTION, not by a check on this path: `candidate` is a
    decoded record, so its `iface` is one interface name (or "", handled on the first line) and its
    `vlan` is a real bool. The name is never re-derived from a raw field here — it used to be
    `str(candidate.get("iface") or "")`, and that coercion is what turned the JSON number `0.9` into a
    name that passes every check below and reaches `ip link delete 0.9`. There is no raw field left to
    reach: `Candidate` refuses subscripting and `.get` outright.
    """
    iface = candidate.iface
    if not iface:
        return UndoOutcome(), False
    other, unaskable = _is_other_than_restored(iface, restored)
    if unaskable:
        # Retained for the same reason as every other unreadable record on this path, and it is the
        # candidate interface itself that the retention keeps covered: the record that cannot be read
        # is the one naming the segment in service, so nothing here can rule out that the candidate IS
        # that segment. Refusing the delete leaves an interface the operator is told about; acting on
        # the inequality removed the one the panel cannot put back.
        reason = (f"the candidate link {iface} was left on the host: the panel's record of the segment "
                  f"it went back to is not an interface name, so nothing can show {iface} is not the "
                  f"interface now in service ({unaskable})")
        return UndoOutcome(actions=[reason], unresolved=[reason]), False
    if not other:
        return UndoOutcome(), False
    claimed, unreadable = _vlan_claim(candidate)
    if unreadable:
        # Retained, not discarded: a record that cannot be read in full may be naming an interface
        # that is up carrying the segment, so it stays pending — which keeps the cover naming it
        # (`pending_candidate_ifaces`) — and the operator is told what is wrong with it.
        return UndoOutcome(actions=[unreadable], unresolved=[unreadable]), False
    if not claimed:
        return UndoOutcome(), False
    prior = candidate.prior
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


def _orphan_pairs(candidate: Candidate, installed: "Candidate | None",
                  restored: dict) -> list[tuple[str, str]]:
    """The `(iface, addr)` pairs a rolled-back pass may have left on the host.

    Addresses the RESTORED ownership metadata still names are excluded — those are in use, not
    orphans. That also keeps the common case quiet: a change that never moved the segment leaves
    the candidate addresses equal to the restored ones, so there is nothing here at all.

    Both sources are needed. `candidate` is what the pass was HEADED for, written before it ran a
    single host command, and `installed` is what it was read back as having claimed — the only
    place a v6 prefix the pass resolved for itself (`auto`/PD) ever appears. Neither proves the
    address is on the host, which is exactly why the undo does not delete them; for the cover the
    weaker claim is the right one, because an address that was never installed is proven absent by
    the first probe (see `drain_enforcement_cover`) and costs one pass of over-covering, while one
    that was installed and left out would be the bypass.

    EVERY VALUE IS DECODED BEFORE IT GETS HERE, NEVER COERCED, and this is where coercion did its
    damage. The values were read as `str(record.get(field) or "")` because a record's may not be text —
    so `addr4: [1]` became the address `"[1]"`, which was reported to the operator, written into the
    survivor ledger as a survivor, and then proven absent by the ledger's very first probe: the
    interface left the enforcement cover while a real segment address may still have been on it, and
    the pending record that was covering it had been cleared as settled. Both arguments are therefore
    `Candidate`s and not dicts: a record that is not the shape the panel writes never decoded into one,
    so the undo stayed pending and the cover went on naming the candidate (see
    `undo_provision_candidate`). There is no field here left to read the other way.

    THE RESTORED PAIRS ARE EXCLUDED IN BOTH SPELLINGS, because they are the one source that is not
    decoded: they come out of the ownership records as text, while the candidates' addresses arrive
    canonicalised. Excluding only the literal text would let a differently-spelled ownership record
    turn the address the recovery pass is USING into a reported orphan and a survivor entry. That is
    the harmless direction — over-covering, and a line the operator can ignore — but it is still
    wrong, and comparing both forms costs nothing.
    """
    keep: set[tuple[str, str]] = set()
    for addr in (restored["addr4"], restored["addr6"]):
        keep.add((restored["iface"], addr))
        spelled = _addr_token(addr)
        if spelled is not None:
            keep.add((restored["iface"], spelled.with_prefixlen))
    named = [] if installed is None else [(installed.iface, installed.addr4),
                                          (installed.iface, installed.addr6)]
    return _distinct(named + [(candidate.iface, candidate.addr4),
                              (candidate.iface, candidate.addr6)], frozenset(keep))


def _report_orphan_addrs(pairs: list[tuple[str, str]]) -> UndoOutcome:
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

    Reported once, never `unresolved` for the REPORT's sake: no later pass will delete these
    either, so keeping the pending record to say the same thing again buys nothing. The link half
    still retries, because there a retry can still finish the job — and so does the ledger write the
    caller performs with these same pairs, which is a different outstanding thing and is allowed to
    keep the record (see `undo_provision_candidate`).

    Reporting is not the only thing done with these pairs, though, and it used to be. An address
    left on an interface that is up is an interface the ruleset has to name, for as long as it is
    there — so the same pairs are recorded in the surviving-candidate ledger and covered until the
    host proves them gone (see `SURVIVOR_KEY`). One is what the operator is told; the other is what
    the panel keeps enforcing meanwhile.
    """
    by_iface: dict[str, list[str]] = {}
    for iface, addr in pairs:
        by_iface.setdefault(iface, []).append(addr)
    return UndoOutcome(actions=[_ORPHAN_ADDRS.format(iface=iface, addrs=", ".join(addrs))
                                for iface, addrs in by_iface.items()])


def undo_provision_candidate(state, candidate: "dict | Candidate | None",
                             installed: "dict | Candidate | None" = None) -> UndoOutcome:
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

    WHAT SURVIVES IS ALSO RECORDED, and that is the half this used to be missing. "The candidate
    link is still on the host and may be carrying the address this change put there" is the
    conclusion of every branch that does not delete — a pre-existing link, one the panel cannot
    prove it created, one whose deletion was refused, one whose probe would not answer — and the
    caller's next act is to render the RESTORED plan, which names the interface it went back to and
    nothing else. So the surviving pairs go into the enforcement cover here, where the decision not
    to remove them is made, and stay there until the host proves them gone. The one branch that
    records nothing is the one that proved the opposite: a link the undo deleted took its addresses
    with it, so its entries are dropped instead.

    AND THAT RECORDING HAS TO BE PROVEN, not attempted. It is the whole basis of the cover, so a
    write that raises — or one that quietly keeps nothing — leaves the interface live and named
    nowhere, while this returns as though coverage were durable and the caller clears the pending
    record and renders the guard. That is the bypass reached through the recovery path, so a
    survivor write that cannot be read back makes the undo UNRESOLVED: the caller keeps the pending
    record, a later pass retries the write, and the cover names the candidate from that record until
    it lands (`pending_candidate_ifaces`).
    """
    run = getattr(state.net, "_run", None)
    if run is None:
        return UndoOutcome()
    # THE BOUNDARY FOR THE DIRECT PATH. The boot path hands in the record it already decoded
    # (`_pending_candidate`); the two in-process rollbacks hand their dicts straight in, so the same
    # decode runs here, before a single value is used for anything — and from here down there are no
    # dicts left, only `Candidate`s, so no reader on this path can reach a raw field. Both records go
    # through it: the candidate authorises the delete, and `installed` is the other source of the pairs
    # that go into the survivor ledger. A record that fails is unknown, not partly usable: nothing is
    # removed, nothing is recorded, the undo stays PENDING and the enforcement keeps covering what that
    # record names, exactly as an unreadable ownership read below does.
    try:
        decoded = _as_candidate(candidate)
        if decoded is None:                 # the pass put nothing anywhere; there is nothing to undo
            return UndoOutcome()
        supplied = _as_candidate(installed)
    except RecordUnknown as exc:
        reason = ("the record of what a rolled-back change may have put on the host is not the record "
                  "the panel writes, so nothing was removed and nothing was recorded; the undo stays "
                  "pending and the enforcement keeps covering what it names: " + str(exc))
        _log.error("%s", reason)
        return UndoOutcome(actions=[reason], unresolved=[reason])
    unreadable, iface, addr4, addr6 = _read_managed_state(state.store)
    if unreadable:
        # WHAT IT KEEPS is whatever the ownership metadata names, so an unreadable one is not a
        # licence to remove anything: the candidate link may be the very interface the recovery pass
        # went back to, and the guard against deleting that one is a comparison with this record.
        # Nothing is touched, the pending record stays, and the cover keeps naming the candidate.
        reason = ("the panel's record of the segment it went back to could not be read, so the host "
                  "state a rolled-back change left behind was neither removed nor recorded; the undo "
                  "stays pending and the enforcement keeps covering "
                  f"{decoded.iface or 'the candidate'}: " + "; ".join(unreadable))
        _log.error("%s", reason)
        return UndoOutcome(actions=[reason], unresolved=[reason])
    restored = {"iface": iface, "addr4": addr4, "addr6": addr6}
    outcome, link_deleted = _undo_candidate_link(state, decoded, restored, run)
    if link_deleted:
        # The link went and took every address on it with it; there is nothing left to report,
        # and nothing left for the enforcement to cover.
        forget_survivors(state.store, decoded.iface)
        return outcome
    surviving = _orphan_pairs(decoded, supplied, restored)
    outcome = _merge(outcome, _report_orphan_addrs(surviving))
    if not remember_survivors(state.store, surviving):
        # The one thing in the address half a later pass CAN still finish, and the reason this
        # branch is `unresolved` while the report above deliberately is not: what is outstanding is
        # the ledger write, not a deletion. Keeping the record buys both halves of the recovery —
        # a retry (`resume_pending_provision_undo` runs before the next pass) and, until it works,
        # the enforcement cover for this very interface (`pending_candidate_ifaces`).
        reason = ("the interfaces a rolled-back change may have left carrying the segment could "
                  "not be recorded (" + ", ".join(sorted({iface for iface, _ in surviving}))
                  + "); the undo stays pending so a later pass can record them, and the "
                  "enforcement covers them from that record until it does")
        outcome.actions.append(reason)
        outcome.unresolved.append(reason)
    return outcome


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
    do better. A record whose work is not finished is KEPT, so the next pass retries it. A record
    already in its TERMINAL form is nothing at all — the same answer as a blank one, quietly, so a
    clear whose blanking did not land does not report a settled undo on every boot. One that merely
    CLAIMS to be settled, in any shape but the exact one the clear writes, is not that: it may still
    be naming an interface a pass put the segment on, so it is retained, not acted on and not cleared
    (see `_settled_undo`).

    A store that cannot be READ is distinguished from one holding an unusable value, and only the
    second is discarded: the first may be holding a perfectly good record naming an interface that is
    up, so nothing is cleared and the enforcement keeps covering it from that record meanwhile. A
    record whose `iface` is not an interface name is on the FIRST side of that line, not the second:
    it says an interface may be live and then says it in something nothing can enforce on, so it is
    kept for the operator to repair rather than cleared (see `_pending_candidate`).
    """
    store = state.store
    pending = _pending_candidate(store)
    if pending.unusable:
        _log.error("discarding an unusable pending host-provisioning undo: %s", pending.unusable)
        clear_provision_candidate(store)
        return UndoOutcome()
    candidate = pending.candidate
    if candidate is None:
        # Settled, a record the store would not read, one that only CLAIMS to be settled, or one whose
        # fields are not the shapes the panel writes. Nothing here may act on any of them and none of
        # them is cleared — but the last three are why the enforcement is refusing to narrow off the
        # interface the record names, and this is where an operator looks for that. Said once per boot.
        if pending.cover.unknown:
            _log.error("the pending host-provisioning undo was neither acted on nor cleared, and the "
                       "enforcement keeps covering what it names: %s", "; ".join(pending.cover.unknown))
        return UndoOutcome()
    _log.warning("a network change was interrupted before its host state could be reclaimed; "
                 "undoing candidate %s", ", ".join(pending.cover.names) or "(unnamed)")
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


def _clear_pd_prefix(store) -> list[str]:
    """Forget the recorded delegated /64, proven. A reason per thing that did not land.

    Called where the pass has just decided the segment is not on a delegated prefix any more — auto
    mode off, or segment management off — and the record is what a LATER pass reads to decide which
    /64 to install. Unverified, a `set_setting` that kept nothing left a stale delegation that the
    next re-enable would install as though it were current.
    """
    why = write_record(store, "pd_segment_prefix6", "")
    if why:
        _log.error("the recorded delegated IPv6 prefix could not be cleared: %s", why)
        return ["the recorded delegated IPv6 prefix could not be cleared, so a later pass may "
                "install a /64 that is no longer delegated: " + why]
    return []


def _provision_result(links: list[str], addrs: list[str] = (), applied: bool = True,
                      enforcement: str = "", records: list[str] = ()) -> NetResult:
    """The pass result, given the host state the panel OWNS and could not remove.

    `applied=False` says the segment addresses in the plan were never installed, and it is reported
    as its own thing, because "the panel owns an address it did not remove" and "the interface does
    not have the address this configuration names" are different failures with different remedies.
    Both fail the pass; only the second means the running host is still on the previous
    configuration.

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
    if records:
        # Its own clause because it is its own failure: the host may be exactly as the configuration
        # says while the panel's record of it is not, and what that costs is every LATER decision
        # taken from the record — which /64 to install, which address is owed a removal, which
        # interface the ruleset still has to name.
        parts.append("durable record not written: " + "; ".join(records))
    if enforcement:
        # The ruleset the host is left with still covers the segment — a transitional one covers
        # MORE than the plan names, never less — so this is not a leak. It is a pass whose
        # enforcement does not match the configuration it is reporting, which is the same class of
        # failure as an address that would not go, and travels the same channel.
        parts.append("segment enforcement not narrowed: " + enforcement)
    if not applied:
        parts.append("segment addresses not applied: "
                     + ("; ".join(addrs) or "no reason given"))
    elif addrs:
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
                # THE PREFIX IS RESOLVED INTO THE PLAN, NOT INTO THE STORE. Only the reconcile
                # below can make it true of the host, so only its success may record it: a write
                # placed before that line survives every way this block can end early — a plan that
                # will not build, a reconcile that raises, a store that rejects the next write —
                # and each of those leaves `pd_segment_prefix6` naming a /64 the segment does not
                # have, reported as a failure while persisted as a fact. Restoring it in an
                # `except` is not the same guarantee: the restore is itself a line that can be
                # skipped. Persisting after the fact needs no restore, because nothing was written.
                # The delegation is not lost either way — the watcher re-reports it on renewal.
                plan = NetPlan.from_store(store, settings)
                plan.segment_ip6 = effective_segment_prefix6(store, settings,
                                                             delegated=selected or "")
                addrs = reconcile_segment_addresses(store, plan, run=run)
                if not addrs.applied:
                    _log.error("the delegated prefix was not applied to the segment, so it was not "
                               "recorded and nothing keyed to it was configured: %s",
                               "; ".join(addrs.reasons))
                    _set_result(state, _provision_result([], addrs.reasons, applied=False))
                    return
                # It is on the interface, so it is now a fact about the host and is recorded as
                # one — PROVABLY, because the record is what every later pass resolves the segment
                # prefix from. A write that quietly kept nothing leaves the delegated /64 on the
                # interface and the store still naming the ULA fallback, so the next pass installs
                # that instead and supersedes a working delegation.
                records = _clear_pd_prefix(store) if not selected else []
                if selected:
                    why = write_record(store, "pd_segment_prefix6", selected)
                    records = ["the delegated IPv6 prefix is on the segment but could not be "
                               "recorded, so a later pass will install the fallback instead: "
                               + why] if why else []
                    if why:
                        _log.error("%s", records[0])
                # THIS WATCHER MAY NOT RAISE THE SEGMENT, and used to. A renewal can land on an
                # interface an earlier FAILED retarget left created and down — and raising it here
                # completes none of the rest of that retarget: the superseded link is still there,
                # NetworkManager still owns the candidate, and the kill-switch drop and tproxy
                # redirect still name the interface being replaced. The bring-up would therefore
                # do exactly what the pass refuses to do, and applying dnsmasq straight afterwards
                # would put clients on it. What a down candidate needs is a full provisioning pass,
                # which is the only thing that can order all of those correctly.
                #
                # So the link is asked about instead, and only a host that says UP gets the rest.
                # `LINK_UNKNOWN` is not a yes: an unanswerable probe is exactly the state a
                # half-finished retarget produces, and pointing dnsmasq at the segment is what
                # makes an interface operational. Skipping it costs a renewal its RA refresh, which
                # the next renewal or pass repeats; the addresses are already installed either way.
                link_state, why = _probe_iface_up(plan.segment_iface, run=run)
                if link_state != LINK_UP:
                    reason = (f"the delegated prefix is on {plan.segment_iface}, but the interface "
                              f"is not up ({why or link_state}), so nothing keyed to it was "
                              "configured; a full provisioning pass has to finish the interface "
                              "change that left it down")
                    _log.error("%s", reason)
                    # Its own clause, not appended to the address reasons: those are removals that
                    # would not go, and this is the segment not being in a state that may be
                    # served. Whatever the reconcile could not settle is still reported with it.
                    settled = _provision_result([], addrs.reasons, records=records).error
                    _set_result(state, NetResult(
                        ok=False, error="; ".join(part for part in (settled, reason) if part)))
                    return
                dnsmasq = getattr(state, "dnsmasq", None)
                if dnsmasq is not None and (store.get_setting("manage_dnsmasq") or "1") == "1":
                    dnsmasq.apply(render_dnsmasq(plan))
                # A superseded address this could not remove is reported here too: the watcher is
                # the one caller with no request to fail, so its result is the only surface.
                _set_result(state, _provision_result([], addrs.reasons, records=records))
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


def _provision_segment(state, run, pd, dnsmasq) -> tuple[NetResult, bool]:
    """Bring the configured segment up on the host. Returns `(result, stop the PD client)`.

    MOVING THE SEGMENT FROM ONE INTERFACE TO ANOTHER IS A SEQUENCE, and this is it:

      1. CREATE the candidate, down                  `ensure_segment_link`
      2. STAGE the enforcement over BOTH interfaces  `apply_enforcement(covering_plan(…))`
      3. REPLACE both addresses                      `reconcile_segment_addresses`
      4. take NM ownership of both                   `ensure_nm_unmanaged(also=…)`
      5. ACTIVATE the candidate  <- THE COMMIT POINT `activate_segment_link`
      6. point dnsmasq at it, then RETIRE the link it supersedes
      7. NARROW the enforcement and the drop-in back to the configured interface alone

    Each step is where it is because of what a failure — or a process killed — between it and the
    next one leaves behind, and the invariant every one of those states has to satisfy is that no
    interface is ever up and reachable without the ruleset covering it.

    The addresses need an interface to land on, so the creation goes first, and it creates the
    link DOWN. The bring-up is the single commit point: before it the candidate carries nothing
    and the previous interface is still the live one; after it the segment is on the new
    interface, which is why everything that would configure a service for the new segment sits
    below it and nothing above it can be undone by a caller. Retirement is later still, because a
    link deleted before the replacement that supersedes it is a link the failure path cannot give
    back — and dnsmasq is moved before it, so no window has the old link deleted while the
    segment's DHCP still names it.

    The enforcement is staged BEFORE any of that. It is the one thing that must already be true of
    an interface the moment it can carry a packet, and the moment differs: a VLAN this pass
    created cannot carry one until step 5, but a candidate that was ALREADY UP when the pass
    arrived — an operator's own interface, which this module may never lower — becomes reachable
    on the segment's address at step 3. Staging above both covers both, and covering both
    interfaces at once is a superset: every intermediate state is over-covered, never under. The
    alternative, refusing the operator's change when the target is already up, puts a "do nothing"
    branch in the middle of a host reconfiguration, which is the shape of defect that has already
    cost this module a segment twice (see `BACKLOG_WARN`).

    Steps 2 and 7 are no-ops when this is not a move: with one interface there is nothing
    transitional to install or narrow, and the pass touches the ruleset not at all. WHAT it stages
    is not the same question as WHETHER it stages, though. The ruleset a move installs covers the
    whole durable cover (`enforcement_cover`), not merely the interface this pass is leaving: a
    rolled-back change may have left a third one up and carrying the segment, and a transitional
    ruleset that named only the two this pass knows about would narrow away from it. Outside a
    move, that third interface is covered by the store-derived render every caller of this pass
    performs straight afterwards (`sync_net`, `boot_guard`, the rollback restores), which is where
    the cover belongs — a pass that touched nothing has nothing to stage.
    """
    store, settings = state.store, state.settings
    stop_pd = False
    ensure_sysctls(settings)
    ensure_segment_prefix6(store, settings)
    plan = NetPlan.from_store(store, settings)
    plan.segment_ip6 = effective_segment_prefix6(store, settings)
    # Both read BEFORE the reconcile, which rewrites the record that answers them. `superseded` is
    # what this pass is moving OFF — the interfaces the panel still holds an address on, which is
    # also what the NM drop-in is written from — and `cover` is everything the ruleset must name,
    # which is that plus anything a rolled-back change left behind.
    #
    # BOTH ARE `Cover`s, AND NEITHER IS ASKED AS A LIST. `superseded` decides WHETHER this pass is a
    # move — so whether the transitional ruleset is staged at all, and what the drop-in hands to
    # NetworkManager — and a names-only view of it cannot tell "the panel holds nothing anywhere
    # else" from "one of the three records would not answer". Spent as the former, and it was: this
    # read used to discard the difference, so a single transient read failure skipped step 2 entirely,
    # and the pass then went on to address and RAISE the candidate under the ruleset the previous
    # configuration left in force — which names only the interface being left. A pass that reported
    # `ok=True`, over the direct-WAN bypass. So the whole answer is kept and read below.
    superseded = superseded_state(store, plan.segment_iface)
    cover = enforcement_cover(store, plan.segment_iface)

    if not superseded.known:
        # NOTHING MOVES ON A HALF-READ ANSWER. This is the record that says whether an interface
        # other than the plan's is still holding a segment address, and an unread source may name one
        # that is up right now: staging, the drop-in and the narrowing decision all read it, and each
        # of them would read the short answer as "there is nothing else". Declining costs this pass —
        # the host is untouched, the live ruleset still covers the segment where it is, and the next
        # pass retries — which is also what the reconcile below does with the same unreadable record,
        # so a store that stays broken loses nothing extra by being refused here first.
        reason = ("the panel could not read which interfaces may still be holding a segment "
                  f"address, so nothing was configured for {plan.segment_iface}: " + superseded.why())
        _log.error("%s", reason)
        return _set_result(state, _provision_result([], [reason], applied=False)), stop_pd

    ensure_segment_link(store, plan, run=run)                                   # 1
    if superseded.names:                                                        # 2
        if not cover.known:
            # A TRANSITIONAL RULESET MAY NOT BE SHORT. It is what covers the candidate from the
            # moment step 3 can make it reachable, and a cover source that would not answer may
            # perfectly well be naming an interface that is already up carrying the segment. So the
            # move does not start: nothing has moved yet, the live ruleset still names the interface
            # the segment is on, and the pass reports instead of raising an interface it cannot
            # promise to have covered.
            reason = ("the enforcement could not be staged over every interface that may be "
                      f"carrying the segment, so it was not moved to {plan.segment_iface}: "
                      + cover.why())
            _log.error("%s", reason)
            return _set_result(state, _provision_result([], [reason], applied=False)), stop_pd
        staged = apply_enforcement(state, covering_plan(plan, cover.names))
        if staged:
            # Nothing has moved yet, so nothing has to be given back: the segment is still on the
            # interface the live ruleset still names, with its address, its dnsmasq and its
            # drop-in. Stopping here is the only safe answer — the next step is the first one that
            # can make the candidate reachable, and there would be no ruleset covering it.
            reason = (f"the enforcement covering {', '.join(cover.names)} and "
                      f"{plan.segment_iface} could not be installed, so the segment was not "
                      f"moved: {staged}")
            _log.error("%s", reason)
            return _set_result(state, _provision_result([], [reason], applied=False)), stop_pd

    addrs = reconcile_segment_addresses(store, plan, run=run)                   # 3
    link_failures: list[str] = []
    records: list[str] = []
    narrow = ""
    # EVERYTHING BELOW IS KEYED TO THE ADDRESSES THIS PLAN NAMES, so none of it runs when they
    # were not installed. dnsmasq is the reason the distinction exists: its DHCP range, router
    # option and listen address all come from `plan.segment_ip`, so applying it over addresses
    # that never landed serves a subnet the interface does not have — and the NetworkManager
    # drop-in would likewise hand the interface the panel still holds an address on back to NM.
    # Such a pass leaves the working configuration of the previous one alone and reports; the
    # caller rolls back.
    if addrs.applied:
        # The addresses are on the new interface, so it may carry traffic and the old link has
        # genuinely been superseded. Not applied means NEITHER: the candidate stays down — it was
        # created down and nothing here raises it — and the previous link is still the live one,
        # keeps its address, keeps the ledger naming both, a true description of the host, which
        # the next pass acts on. The staged ruleset covers it either way.
        # `superseded.names` is the whole set here, proven above: taking an interface away from
        # NetworkManager on a short list is how the one the panel still addresses gets handed back.
        ensure_nm_unmanaged(plan.segment_iface, run=run, also=superseded.names)  # 4
        activate_segment_link(plan, run=run)                                    # 5
        if dnsmasq is not None and (store.get_setting("manage_dnsmasq") or "1") == "1":
            dnsmasq.apply(render_dnsmasq(plan))                                 # 6
        elif dnsmasq is not None:
            dnsmasq.stop()
        link_failures = retire_superseded_links(store, plan, run=run)
        # Narrowing is what ENDS the cover, so it may only happen once nothing it would uncover
        # can still be carrying the segment, and there are several ways one can be. A panel-created
        # link that would not go is still on the host with everything that was on it. An interface
        # the panel did NOT create is never deleted at all — an operator's own — so what decides
        # for that one is whether the address the pass superseded actually came off it: a refused
        # `ip addr del` leaves the old segment address exactly where it was, on an interface that
        # is up, which is the state the ledger records and retries. And a candidate a rolled-back
        # change left behind is neither, and is answered by the host through its own probes. Asking
        # the cover again is asking all of them at once, and it is asked AFTER the retirement,
        # because that is what turns them into answers. Either way the transitional ruleset stays
        # until a later pass finishes the job. Over-covering an interface that no longer exists is
        # free; uncovering one that is still live is the defect.
        #
        # The drop-in narrows on the smaller set, deliberately. Being named in a rule costs an
        # interface nothing, so the ruleset waits for the whole cover; being listed as unmanaged
        # takes an interface away from NetworkManager, so the drop-in waits only for the ones the
        # panel actually holds an address on and hands back anything else at the first opportunity.
        #
        # AND NEITHER SET IS ASKED AS A LIST. Both are records, both can fail to answer, and both
        # decisions here are "stop covering something": an unreadable source that came back as an
        # empty list read as "nothing left" and narrowed off exactly the interface the record it
        # could not read existed to keep named. `may_narrow` is true only when every source answered
        # AND named nothing; anything else leaves the transitional ruleset — a superset — in place
        # and reports, so a later pass finishes the job.
        # `superseded.names` is what says this pass staged something to narrow BACK from, and it is
        # the proven set — `Cover.__bool__` raises, so neither of these decisions can be taken from
        # the object as a whole and read "no answer" as "not a move".
        managed = superseded_state(store, plan.segment_iface)
        remaining = enforcement_cover(store, plan.segment_iface)
        if superseded.names and managed.may_narrow:                             # 7 (the drop-in)
            ensure_nm_unmanaged(plan.segment_iface, run=run)
        if superseded.names and remaining.may_narrow:                           # 7 (the ruleset)
            narrow = apply_enforcement(state, plan)
        elif superseded.names and not remaining.known:
            narrow = ("the enforcement was left covering every interface it already named, because "
                      "the panel could not prove nothing else is still carrying the segment: "
                      + remaining.why())
            _log.error("%s", narrow)
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
                records += _clear_pd_prefix(store)
    else:
        _log.error("the segment addresses this configuration names are not on the interface, so "
                   "nothing keyed to them was configured: %s", "; ".join(addrs.reasons))
    # The rest of the pass still runs: the new segment must come up even when a superseded link or
    # address refuses to go, and the pass then reports that leftover — a config the host does not
    # match must not commit as a success.
    return _set_result(state, _provision_result(link_failures, addrs.reasons, records=records,
                                                applied=addrs.applied, enforcement=narrow)), stop_pd


def host_provision(state) -> NetResult:
    """Idempotent host gateway bring-up. Gated on the linux backend + `manage_segment`.
    Never raises out — a provisioning failure is logged, not fatal to boot. Re-entrant under
    the controller apply-lock so it can't interleave with a tunnel apply.

    IT IS ALSO WHERE THE EMERGENCY DENY IS HANDED BACK. A boot that could not install the panel's
    own enforcement holds the forward path closed with a ruleset that names no interface, and
    nothing narrows or drains such a ruleset — the only thing that ends it is a render that proves a
    complete cover. This pass runs on boot, on `PUT /api/network` and on a restore, which is every
    path that can change that answer, so the handover is attempted here, after the pass and whatever
    it managed to settle (see `_hand_back_from_emergency_deny`).
    """
    store = state.store
    if not _is_linux_backend(state.net):
        return _set_result(state, NetResult(ok=True))
    from pi_gw_panel.controller import apply_lock
    run = getattr(state.net, "_run", _run)
    pd = getattr(state, "pd_client", None)
    stop_pd = False
    with apply_lock:
        try:
            # Before anything reads the cover, give the host a chance to shrink it: a candidate a
            # rolled-back change left behind leaves the moment the host says its link is gone or
            # its address is off it. This is the ONLY thing that shrinks that ledger, so it runs on
            # both branches below — with segment management off, this pass is all there is.
            drain_enforcement_cover(store, run)
            dnsmasq = getattr(state, "dnsmasq", None)
            if (store.get_setting("manage_segment") or "1") != "1":
                stop_pd = True
                records = _clear_pd_prefix(store)
                if dnsmasq is not None:
                    dnsmasq.stop()
                addr_failures = clear_managed_addresses(store, run=run)
                link_failures = clear_managed_link(store, run=run)
                remove_nm_unmanaged(run=run)
                result = _set_result(state, _provision_result(link_failures, addr_failures,
                                                             records=records))
            else:
                result, stop_pd = _provision_segment(state, run, pd, dnsmasq)
        except Exception as exc:    # never crash boot on a provisioning hiccup
            _log.warning("host_provision failed: %s", exc)
            result = _set_result(state, NetResult(ok=False, error=str(exc)))
        # ONE report per pass of what the pass is keeping verbatim and cannot heal, said here rather
        # than at each of the five reads of the record that holds it. After the pass, deliberately:
        # what is left in the ledger now is what the operator has to go and look at, and a read that
        # decides nothing may not be the one that spends a transient store fault the decisions below
        # would have declined on (see `_report_retained_ownership`).
        _report_retained_ownership(store)
        # Attempted whatever the pass above did, and reported through the same `ok=False` channel as
        # every other host state the pass intended to reach and did not: while the deny is in force
        # the segment has no network at all, so a pass that leaves it there has not reached the
        # configuration it would otherwise be reporting as applied.
        held = _hand_back_from_emergency_deny(state)
        if held:
            result = _set_result(state, NetResult(
                ok=False, error="; ".join(p for p in (result.error, held) if p)))
    # Outside the apply-lock on purpose: the PD watcher takes that same lock inside its
    # callback, so joining its thread while holding it would block for the whole join
    # timeout. The store state that makes a late callback a no-op is already committed above.
    if stop_pd and pd is not None:
        _stop_pd(pd)
    return result
