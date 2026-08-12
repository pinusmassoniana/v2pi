"""The durable-record primitives: the one place a settings write becomes a fact.

Everything `net_control/provision.py` remembers between passes is a row in the settings k/v store,
and every one of those records is load-bearing: the enforcement cover is derived from them, the links
and addresses the panel owns are retired from them, and a record that stops describing the host is how
an interface ends up live and outside the ruleset.

The store gives no transactional guarantee and no typed answer, so each call site used to verify its
own writes — and each got it wrong differently. A failed read returned as an empty record, so
"unknown" was spent as "there is nothing to cover". A write that replaced a set was verified against
the entries just requested, so a truncation that kept the newest and dropped the oldest passed. A
clear took a non-raising write as proof of deletion. Those are one missing abstraction, not three
bugs, and this is the abstraction's own test: the failure modes a k/v store really has, asked of
`read_record` / `write_record` / `read_set` / `write_set` / `clear_record` directly.
"""
import pytest

from pi_gw_panel.net_control import provision

KEY = "a_durable_record"
PAIRS = ["eth0.7 192.168.8.2/24", "eth0.9 192.168.10.2/24"]


class _Store:
    """A settings store that fails the ways a real one can, per key.

    `write_raises` and `read_raises` are the loud failures. `write_noop` is the quiet one — the write
    reports success and keeps nothing, which no caller can see without reading the record back.
    `keep_last` truncates a multi-line value to its final line: the partial write, in the shape a set
    record actually takes, where what survives is always the newest entry.
    """

    def __init__(self, write_raises=(), write_noop=(), read_raises=(), keep_last=()):
        self.values: dict[str, str] = {}
        self.write_raises, self.write_noop = set(write_raises), set(write_noop)
        self.read_raises, self.keep_last = set(read_raises), set(keep_last)

    def get_setting(self, key):
        if key in self.read_raises:
            raise RuntimeError("simulated settings read failure")
        return self.values.get(key)

    def set_setting(self, key, value):
        if key in self.write_raises:
            raise RuntimeError("simulated settings write failure")
        if key in self.write_noop:
            return
        if key in self.keep_last:
            lines = [line for line in value.splitlines() if line.strip()]
            value = lines[-1] if lines else value
        self.values[key] = value


# --- read: unknown is not empty, and cannot be mistaken for it --------------------------------

def test_a_record_that_cannot_be_read_is_unknown_and_says_why():
    store = _Store(read_raises=[KEY])

    record = provision.read_record(store, KEY)

    assert record.known is False
    assert "simulated settings read failure" in record.reason


def test_an_unknown_record_refuses_to_hand_over_content():
    """The point of the type. Every accessor raises, so a caller that skipped the guard fails on the
    line that mattered instead of carrying on with a plausible empty answer."""
    record = provision.read_record(_Store(read_raises=[KEY]), KEY)

    for read in (lambda: record.text, record.lines, record.pairs):
        with pytest.raises(provision.RecordUnknown):
            read()


def test_an_unknown_record_is_not_a_truth_value_and_does_not_equal_an_empty_one():
    """`if record:` cannot pick one of the two facts by accident, and the two facts do not compare
    equal — which is what stops "no answer" being handled by the branch written for "nothing there"."""
    unknown = provision.read_record(_Store(read_raises=[KEY]), KEY)
    empty = provision.read_record(_Store(), KEY)

    with pytest.raises(TypeError):
        bool(unknown)
    assert empty.known is True and empty.lines() == []
    assert unknown != empty
    assert unknown != [] and unknown != ""


def test_a_record_that_is_simply_absent_reads_as_a_known_empty_one():
    """The other half: a key that was never written is an ANSWER, and an empty one. Treating that as
    unknown would keep every ledger permanently unreadable on a fresh install."""
    record = provision.read_record(_Store(), KEY)

    assert record.known is True
    assert record.text == "" and record.lines() == [] and record.pairs() == []


def test_a_set_record_reads_its_entries_in_order_without_blanks_or_repeats():
    store = _Store()
    store.set_setting(KEY, "  eth0.7 192.168.8.2/24\n\neth0.7 192.168.8.2/24\neth0.9 10.9.9.1/24\n")

    record = provision.read_record(store, KEY)

    assert record.lines() == ["eth0.7 192.168.8.2/24", "eth0.9 10.9.9.1/24"]
    assert record.pairs() == [("eth0.7", "192.168.8.2/24"), ("eth0.9", "10.9.9.1/24")]


# --- ...and content that cannot be read IN FULL is unknown too ---------------------------------

def test_a_record_holding_an_entry_that_is_not_a_pair_is_unknown_not_partly_known():
    """The same collapse one level down. `pairs()` used to skip a malformed nonblank entry and leave
    the record KNOWN, so a truncated ledger answered "I can see everything, and there is nothing" —
    which is the one answer that licenses narrowing the enforcement."""
    store = _Store()
    store.set_setting(KEY, "eth0.2\n")              # the address half was lost

    record = provision.read_record(store, KEY)

    assert record.known is True and record.lines() == ["eth0.2"]
    with pytest.raises(provision.RecordUnknown) as caught:
        record.pairs()
    assert "eth0.2" in str(caught.value), "the reason has to name the entry an operator must fix"


def test_a_truncated_ledger_entry_keeps_the_interface_it_names_covered():
    """PRODUCT-CRITICAL, and the whole point of the rule. The stale ledger is the only record that a
    panel-owned address is still on an interface the pass is leaving, so a truncated entry there must
    forbid narrowing — `Cover(known=True, names=[])` is what uncovers a live interface."""
    store = _Store()
    store.set_setting(provision.STALE_KEY, "eth0.2\n")

    cover = provision.enforcement_cover(store, "eth0.9")
    superseded = provision.superseded_state(store, "eth0.9")

    for state in (cover, superseded):
        assert state.known is False, "a ledger that cannot be read in full read as a complete answer"
        assert state.may_narrow is False
        assert "eth0.2" in state.why()


def test_a_truncated_survivor_ledger_is_neither_drained_nor_replaced():
    """The three callers that may not raise and may not narrow, asked the same question. Each keeps
    every pair exactly where it is, which over-covers whatever is already gone — free — instead of
    computing a new set out of a parse that dropped an entry."""
    store = _Store()
    store.set_setting(provision.SURVIVOR_KEY, "eth1 192.168.9.2/24\neth0.2\n")

    assert provision.drain_enforcement_cover(store, run=lambda *a, **k: None) == []
    assert provision.remember_survivors(store, [("eth0.9", "192.168.10.2/24")]) is False
    provision.forget_survivors(store, "eth1")

    assert store.values[provision.SURVIVOR_KEY] == "eth1 192.168.9.2/24\neth0.2\n"
    assert provision._read_ownership(store).known, \
        "only the ledger that is malformed may be reported unreadable"


def test_a_truncated_stale_entry_stops_the_pass_installing_over_the_record_it_cannot_read():
    """The ownership read is the one that decides whether addresses go on at all, and the ledger it
    would rewrite is computed from this parse: an entry that did not parse is an address on the host
    whose last record this pass would drop. So it reports, quoting the entry, and installs nothing."""
    store = _Store()
    store.set_setting(provision.STALE_KEY, "eth0.2 192.168.9.2/24\neth0.7\n")

    own = provision._read_ownership(store)

    assert not own.known and "eth0.7" in own.unreadable[0]
    assert (own.iface, own.addr4, own.addr6, own.stale, own.retained) == ("", "", "", (), ())


# --- write: proven by the read-back, never by the absence of an exception ----------------------

def test_a_write_that_raises_is_reported():
    why = provision.write_record(_Store(write_raises=[KEY]), KEY, "eth0.9")

    assert "could not be written" in why and KEY in why


def test_a_write_that_quietly_keeps_nothing_is_reported():
    """THE failure the read-back exists for: `set_setting` returns, and the record is unchanged."""
    store = _Store()
    store.set_setting(KEY, "eth0.2")
    store.write_noop = {KEY}                        # ...and from here it keeps nothing, silently

    why = provision.write_record(store, KEY, "eth0.9")

    assert why and "eth0.9" in why
    assert store.values[KEY] == "eth0.2", "the store was supposed to have kept the old value"


def test_a_write_that_cannot_be_read_back_is_reported():
    """Not proof either: a store that took the write and will not answer for it has told us nothing.
    """
    store = _Store(read_raises=[KEY])

    why = provision.write_record(store, KEY, "eth0.9")

    assert "could not be read back" in why


def test_a_clean_write_is_proven_and_reports_nothing():
    store = _Store()

    assert provision.write_record(store, KEY, "eth0.9") == ""
    assert provision.read_record(store, KEY).text == "eth0.9"


# --- write_set: the COMPLETE expected content, and a journal so a partial write loses nothing ---

def test_a_set_write_verifies_every_entry_and_not_merely_the_newest():
    """The defect verifying the delta cannot see. The record keeps only its last line, which is
    exactly the entry a caller that just added one would check for."""
    store = _Store(keep_last=[KEY])

    why = provision.write_set(store, KEY, PAIRS)

    assert why and PAIRS[0] in why, "a write that dropped the older entry was reported as clean"
    assert store.values[KEY] == PAIRS[1]


def test_a_partially_written_set_still_reads_back_complete_from_its_journal():
    """And reporting is not enough. The dropped entry is one nothing will ask for again, so the
    complete content is journalled before the record is replaced and the two are read as one: the
    entry stays covered, not merely mourned."""
    store = _Store(keep_last=[KEY])

    assert provision.write_set(store, KEY, PAIRS) != ""

    record = provision.read_set(store, KEY)
    assert record.known is True
    assert set(record.lines()) == set(PAIRS)


def test_a_journal_that_cannot_be_written_leaves_the_record_untouched():
    """The journal goes first for this reason: until it holds the whole set, the record it protects
    has not been touched, so a failure there loses nothing at all."""
    store = _Store(write_raises=[provision._journal_key(KEY)])
    store.set_setting(KEY, PAIRS[0])

    why = provision.write_set(store, KEY, PAIRS)

    assert why and provision._journal_key(KEY) in why
    assert provision.read_set(store, KEY).lines() == [PAIRS[0]]


def test_a_proven_set_write_leaves_no_journal_behind():
    """The journal may not become a second ledger nothing empties: once the record holds everything,
    it is the single answer again."""
    store = _Store()

    assert provision.write_set(store, KEY, PAIRS) == ""
    assert provision.read_set(store, KEY).lines() == PAIRS
    assert store.values.get(provision._journal_key(KEY), "") == ""


def test_a_set_is_unknown_when_either_half_cannot_be_read():
    """An unreadable half is not an empty half — in either position, because a caller cannot tell
    which one it got and both are the whole answer."""
    for read_raises in ([KEY], [provision._journal_key(KEY)]):
        store = _Store(read_raises=read_raises)
        assert provision.read_set(store, KEY).known is False


def test_shrinking_a_set_to_nothing_is_proven_too():
    """The direction a drain uses. A write that keeps the entries it was told to drop is reported —
    over-covering, which is free, and a record that does not describe the host, which is not."""
    store = _Store()
    assert provision.write_set(store, KEY, PAIRS) == ""

    store.write_noop = {KEY}
    why = provision.write_set(store, KEY, [])

    assert why and provision.read_set(store, KEY).lines() == PAIRS


# --- the ownership GROUP: one write-ahead step, proven, before any scalar moves -----------------
#
# The four ownership records only mean anything together, and they do not hold the same news: the
# three scalars say where the segment is going, the stale ledger is the only record of the pair it is
# coming OFF. Verified separately and written scalars-first, a store that took the scalars and then
# refused the ledger's journal reported failure — after persisting the candidate as the segment's
# interface with the old live pair recorded nowhere at all. Nothing about that snapshot looks broken
# from the inside, which is why no later pass would have gone back for it.

def _ownership_store(**extra) -> _Store:
    """A store already recording a live segment on `eth0.2`, the pair a move must not lose."""
    store = _Store(**extra)
    store.values.update({"managed_segment_iface": "eth0.2",
                         "managed_segment_addr4": "192.168.9.2/24",
                         "managed_segment_addr6": ""})
    return store


def test_the_stale_superset_is_written_and_proven_before_any_scalar_moves():
    """PRODUCT-CRITICAL. The journal of the write-ahead step will not take the write, so the group
    has to abort with the host's real state still recorded: the old interface and its address."""
    journal = provision._journal_key(provision.STALE_KEY)
    store = _ownership_store(write_noop=[journal])

    reasons = provision._record_ownership(store, "eth0.9", "192.168.10.2/24", "",
                                          [("eth0.2", "192.168.9.2/24")])

    assert reasons and journal in reasons[0]
    assert store.values["managed_segment_iface"] == "eth0.2", "a scalar moved before the ledger did"
    assert store.values["managed_segment_addr4"] == "192.168.9.2/24"
    assert store.values.get(provision.STALE_KEY, "") == ""


def test_an_aborted_ownership_write_still_records_the_old_live_pair():
    """The same failure asked the way the enforcement asks it. Whatever else is true, the interface
    that is still up carrying the segment has to be nameable — by the cover, the NM drop-in and the
    next pass's retry list — and after the abort it is, through the scalars that never moved."""
    store = _ownership_store(write_noop=[provision._journal_key(provision.STALE_KEY)])

    assert provision._record_ownership(store, "eth0.9", "192.168.10.2/24", "",
                                       [("eth0.2", "192.168.9.2/24")])

    assert provision.superseded_state(store, "eth0.9").names == ["eth0.2"]
    own = provision._read_ownership(store)
    assert (own.known, own.iface, own.addr4) == (True, "eth0.2", "192.168.9.2/24")


def test_a_no_op_on_the_stale_journal_leaves_the_whole_group_untouched():
    """The quiet half, asked of the journal key by name. A store that takes the write and keeps
    nothing must not get past this step — nothing downstream re-checks it — and until it is past,
    NOTHING has changed: not the ledger, not the journal, not one scalar."""
    store = _ownership_store(write_noop=[provision._journal_key(provision.STALE_KEY)])
    before = dict(store.values)

    reasons = provision._record_ownership(store, "eth0.9", "192.168.10.2/24", "",
                                          [("eth0.2", "192.168.9.2/24")])

    assert reasons
    assert store.values == before, "the group wrote something after its write-ahead step failed"


def test_a_group_with_nothing_to_supersede_is_not_held_up_by_the_same_no_op():
    """...and the boundary that keeps that from becoming a refusal to work at all: with no pair to
    carry over there is nothing to write ahead, so an empty journal that keeps nothing has lost
    nothing, and the scalars move. A first pass on a fresh host is exactly this case."""
    store = _Store(write_noop=[provision._journal_key(provision.STALE_KEY)])

    assert provision._record_ownership(store, "eth0.9", "192.168.10.2/24", "", []) == []

    assert store.values["managed_segment_iface"] == "eth0.9"
    assert provision.read_set(store, provision.STALE_KEY).pairs() == []


def test_a_provable_group_write_records_all_four_and_reports_nothing():
    """The clean path, unchanged: the ledger holds the superseded pair and the scalars name the
    segment's new interface and addresses."""
    store = _ownership_store()

    assert provision._record_ownership(store, "eth0.9", "192.168.10.2/24", "fd00:1:2:3::1/64",
                                       [("eth0.2", "192.168.9.2/24")]) == []

    assert provision.read_set(store, provision.STALE_KEY).pairs() == [("eth0.2", "192.168.9.2/24")]
    own = provision._read_ownership(store)
    assert own.known and (own.iface, own.addr4, own.addr6) == ("eth0.9", "192.168.10.2/24",
                                                              "fd00:1:2:3::1/64")
    assert own.stale == (("eth0.2", "192.168.9.2/24"),)
    assert own.invalid == () and own.retained == ()


def test_a_scalar_that_will_not_write_is_reported_with_the_ledger_already_in_place():
    """The remaining failures are scalars, and what they leave behind is the over-recording
    direction: the pair is named by the ledger AND still by the current-address keys, which costs a
    retried delete and never a lost address."""
    store = _ownership_store(write_noop=["managed_segment_iface"])

    reasons = provision._record_ownership(store, "eth0.9", "192.168.10.2/24", "",
                                          [("eth0.2", "192.168.9.2/24")])

    assert reasons and "managed_segment_iface" in reasons[0]
    assert provision.read_set(store, provision.STALE_KEY).pairs() == [("eth0.2", "192.168.9.2/24")]
    assert provision.superseded_state(store, "eth0.9").names == ["eth0.2"]


# --- ...and the SHAPE of the interface scalar, which is now proven on this path too --------------
#
# The same scalar the undo reads (`_read_managed_state`), and it used to be checked only there, on the
# argument that here it merely becomes an `ip addr del … dev <name>` target and a ledger entry: a name
# that is not a name matches no device, while refusing would cost the segment its addresses on every
# later pass — this read gates installing them — and retaining the value would write a three-token
# ledger entry that wedges the gateway for good.
#
# Both halves of that were true and the conclusion still did not follow, because there is a third
# option: DISTINGUISH INVALID FROM BLANK and act on neither. Blank means the panel claims nothing and
# the plan's interface may be substituted; invalid means the recorded addresses are on SOMETHING and
# the one thing that cannot be concluded is that it is the interface being installed onto — so no pair
# is formed, nothing is compared, nothing is deleted, the value is reported, and the pass carries on
# and heals the scalar. Nothing is refused but the one action the value would have authorised, and
# `_record_ownership` refuses to write a ledger it could not read back, so the unparseable state that
# argument feared is unreachable rather than tolerated.

class _Iproute2:
    """A `run` seam that speaks iproute2 about the interfaces it has. Absence is an explicit
    not-found on stderr, which is how the real one presents it and the only answer that proves an
    address gone."""

    def __init__(self, on=("eth0.2",), addrs=None):
        self.on, self.cmds = set(on), []
        self.addrs = {iface: set(cidrs) for iface, cidrs in (addrs or {}).items()}

    def __call__(self, cmd, **kw):
        import subprocess
        self.cmds.append(list(cmd))
        rest = [tok for tok in cmd[1:] if tok not in ("-4", "-6", "-o")]
        dev = rest[-1]
        if dev not in self.on:
            raise subprocess.CalledProcessError(1, cmd, stderr=f'Device "{dev}" does not exist.')
        if rest[:2] == ["addr", "show"]:
            body = " ".join(f"inet {a} scope global" for a in sorted(self.addrs.get(dev, ())))
            return subprocess.CompletedProcess(cmd, 0, f"2: {dev} {body}", "")
        if rest[:2] == ["addr", "del"]:
            self.addrs.setdefault(dev, set()).discard(rest[2])
        return subprocess.CompletedProcess(cmd, 0, "", "")


def test_an_ownership_interface_that_is_not_a_name_deletes_nothing_and_blocks_nothing():
    """PRODUCT-CRITICAL both ways. No address comes off a real interface, no `ip addr del` is issued at
    all now — the value never becomes a target — and the pass is not left refusing for ever: it reports
    the value, rewrites the scalars, and the next pass reads a ledger it can parse. (What such a value
    may NOT do is answer a question about a candidate link: see `_read_managed_state`.)"""
    store = _Store()
    store.values.update({"managed_segment_iface": "eth0.2 eth0.9",
                         "managed_segment_addr4": "192.168.9.2/24", "managed_segment_addr6": ""})
    run = _Iproute2(on=("eth0.2",), addrs={"eth0.2": ["192.168.9.2/24"]})

    reasons = provision.clear_managed_addresses(store, run=run)

    assert run.addrs["eth0.2"] == {"192.168.9.2/24"}, "an address was deleted off a real interface"
    assert not [cmd for cmd in run.cmds if "del" in cmd], \
        "a delete was aimed at a device token derived from a value the panel did not write"
    assert reasons and "eth0.2 eth0.9" in reasons[0], "the value was not reported to the operator"
    assert provision._read_ownership(store).known, \
        "the pass left a record no later one can read, so no address can be installed again"
    assert provision.read_set(store, provision.STALE_KEY).pairs() == []
    assert store.values["managed_segment_iface"] == ""


def test_the_ledger_is_not_written_at_all_when_it_would_not_read_back():
    """PRODUCT-CRITICAL, and what makes the state above unreachable rather than merely survivable. A
    pair whose interface carries a space serialises to THREE tokens, and a three-token entry makes
    every later read of the record refuse it in full — which is the read that gates installing and
    removing the segment's addresses. So it is checked against the parse that will read it, BEFORE the
    write-ahead step: nothing is written, no scalar moves, and the reason names the entry."""
    store = _ownership_store()
    before = dict(store.values)

    reasons = provision._record_ownership(store, "eth0.9", "192.168.10.2/24", "",
                                          [("eth0.2 eth0.9", "192.168.9.2/24")])

    assert reasons and "eth0.2 eth0.9" in reasons[0]
    assert store.values == before, "a ledger that cannot be read back was written anyway"
    assert provision._read_ownership(store).known


def test_a_ledger_pair_of_the_wrong_type_is_refused_rather_than_coerced():
    """`f"{iface} {addr}"` on a value of the wrong type is exactly the coercion this module refuses
    everywhere else: `str(0.9)` is a plausible interface name and `str([1])` a plausible address."""
    store = _ownership_store()

    reasons = provision._record_ownership(store, "eth0.9", "192.168.10.2/24", "",
                                          [(0.9, "192.168.9.2/24")])

    assert reasons and "0.9" in reasons[0]
    assert store.values.get(provision.STALE_KEY, "") == ""


def test_a_ledger_entry_the_boundary_could_not_read_is_kept_verbatim_and_acted_on_never(caplog):
    """A pair whose interface is not a name parses as a PAIR, so it is not the wedging shape — and it
    may be the only record of an address on the host. It is retained exactly as it came, reported to the
    operator, and never compared, deleted or rebuilt from parts; the entries beside it are unaffected.
    It is deliberately NOT in `invalid`: the panel cannot heal an entry it must preserve, so failing the
    pass on one would refuse the operator's change on every later pass too.

    THE DECODE ITSELF IS SILENT. This boundary is read about five times in one pass, and it used to log
    every retained entry on each read — five identical ERRORs for one durable fact. The reporting is the
    pass's, once, at its own boundary (`_report_retained_ownership`; see the once-per-pass test over
    `host_provision` in test_provision.py)."""
    store = _Store()
    store.values.update({"managed_segment_iface": "eth0.2",
                         "managed_segment_addr4": "192.168.9.2/24", "managed_segment_addr6": "",
                         provision.STALE_KEY: "eth0.7 192.168.8.2/24\neth0.2/x 192.168.7.2/24"})

    with caplog.at_level("ERROR"):
        own = provision._read_ownership(store)
        provision._read_ownership(store)

    assert own.known and own.stale == (("eth0.7", "192.168.8.2/24"),)
    assert own.retained == (("eth0.2/x", "192.168.7.2/24"),)
    assert own.invalid == (), "an entry the panel must keep would refuse every later pass"
    assert caplog.text == "", "the decode narrated a durable fact once per read of it"

    with caplog.at_level("ERROR"):
        provision._report_retained_ownership(store)

    assert caplog.text.count("eth0.2/x 192.168.7.2/24") == 1, \
        "the entry the panel cannot heal was not reported to the operator at all"

    assert provision._record_ownership(store, "eth0.2", "192.168.9.2/24", "",
                                       list(own.stale), own.retained) == []
    assert provision.read_set(store, provision.STALE_KEY).pairs() == [
        ("eth0.7", "192.168.8.2/24"), ("eth0.2/x", "192.168.7.2/24")]


def test_an_unreadable_ledger_entry_never_stops_a_later_pass_provisioning():
    """NOT WEDGING, asked directly. The entry is preserved on every pass, so if it failed the pass it
    would fail all of them — the outage the whole invalid-versus-blank distinction exists to avoid.
    Provisioning keeps working around it, and the entry stays exactly where it is."""
    store = _Store()
    store.values.update({"managed_segment_iface": "eth0.2",
                         "managed_segment_addr4": "192.168.9.2/24", "managed_segment_addr6": "",
                         provision.STALE_KEY: "eth0.2/x 192.168.7.2/24"})
    run = _Iproute2(on=("eth0.2",), addrs={"eth0.2": ["192.168.9.2/24"]})

    for _ in range(2):
        assert provision.clear_managed_addresses(store, run=run) == [], \
            "an entry the panel cannot heal refused the pass, and would refuse every later one"

    assert provision.read_set(store, provision.STALE_KEY).pairs() == [
        ("eth0.2/x", "192.168.7.2/24")]
    assert not [cmd for cmd in run.cmds if "del" in cmd and "eth0.2/x" in cmd]


# --- ...and the TYPE of a scalar, which was still answered with the collapsed answer ------------
#
# The shape check above tells invalid from blank. The TYPE check did not, and it sat one gate earlier:
# `read_record` maps every non-text value to UNKNOWN, and the three ownership scalars were read through
# it. That mapping is right for a LEDGER — its entries are kept verbatim, so the panel cannot heal one
# and "the store would not answer" costs exactly what "the store answered with rubbish" costs — and
# wrong for a scalar the pass rewrites in full. A `managed_segment_iface` holding a number (a
# hand-edited row, a foreign writer, a column that is not TEXT) made the whole `Ownership` unreadable,
# `_ownership_cover` raise and `_provision_segment` decline BEFORE the reconcile that would have
# rewritten it — so the refusal was permanent. The same wedge the invalid-versus-blank distinction
# closes for text, reached through the type check instead of the shape check.
#
# `_read_scalar` separates the two facts: a store fault stays `unreadable` and refuses, and
# returned-invalid data is `invalid` — no name, no pair, no comparison, no delete, reported, and
# overwritten by the pass on its way past.

_NOT_TEXT = [0, False, [], {}, 0.0, 7, b"eth0.2", ("eth0.2",), None.__class__]


@pytest.mark.parametrize("value", _NOT_TEXT)
def test_a_scalar_the_store_answered_with_that_is_not_text_is_invalid_not_unreadable(value):
    """Both facts at once: the RECORD is unknown, so nothing can take `.text` off it and get a
    plausible blank, and the REASON comes back in the invalid channel — which authorises nothing and is
    healed, rather than the unreadable one, which refuses."""
    store = _Store()
    store.values[KEY] = value

    record, invalid = provision._read_scalar(store, KEY)

    assert record.known is False, "a value that is not text was handed over as though it were"
    assert invalid and repr(value) in invalid and KEY in invalid
    for read in (lambda: record.text, record.lines, record.pairs):
        with pytest.raises(provision.RecordUnknown):
            read()


def test_a_store_that_will_not_answer_a_scalar_is_still_unreadable_and_not_invalid():
    """The line that has to hold while the rest of this loosens. A store fault says nothing about the
    host, so it is the one of the two that still refuses — and it may not arrive as bad data the pass
    would cheerfully overwrite."""
    record, invalid = provision._read_scalar(_Store(read_raises=[KEY]), KEY)

    assert record.known is False and invalid == ""
    assert "simulated settings read failure" in record.reason


@pytest.mark.parametrize("key", provision.OWNERSHIP_KEYS)
def test_a_non_text_ownership_scalar_is_read_and_reported_and_never_refuses(key):
    """Through the boundary, for each of the three scalars: the record still READS, so every pass that
    depends on it runs, and the value is named to the operator instead."""
    store = _ownership_store()
    store.values[key] = 0

    own = provision._read_ownership(store)

    assert own.known is True, "a non-text scalar refused every pass, including the one that heals it"
    assert len(own.invalid) == 1 and key in own.invalid[0]
    assert own.stale == () and own.retained == ()
    assert provision._invalid_ownership_reasons(own), "the value was not reported to the operator"


def test_a_non_text_interface_scalar_authorises_no_deletion():
    """PRODUCT-CRITICAL, and the whole cost of the distinction: `0` under the interface key is not an
    interface, so no pair is formed from it, no `ip addr del` is aimed anywhere, and the address the
    record's value stood for is left exactly where it is and named to the operator."""
    store = _Store()
    store.values.update({"managed_segment_iface": 0,
                         "managed_segment_addr4": "192.168.9.2/24", "managed_segment_addr6": ""})
    run = _Iproute2(on=("eth0.2",), addrs={"eth0.2": ["192.168.9.2/24"]})

    reasons = provision.clear_managed_addresses(store, run=run)

    assert not [cmd for cmd in run.cmds if "del" in cmd], \
        "a delete was aimed at a device token derived from a value that is not even text"
    assert run.addrs["eth0.2"] == {"192.168.9.2/24"}
    assert reasons and "managed_segment_iface" in reasons[0]
    assert provision._read_ownership(store).known, "the pass left a record no later one can read"
    assert store.values["managed_segment_iface"] == ""       # ...and it is healed on the way past


def test_a_store_fault_on_an_ownership_scalar_still_deletes_nothing():
    """The same absence of a delete, for the reason that still refuses. Unreadable is not invalid: here
    the pass declines the removal entirely and says so, rather than rewriting a record it never read."""
    store = _ownership_store(read_raises=["managed_segment_addr4"])
    run = _Iproute2(on=("eth0.2",), addrs={"eth0.2": ["192.168.9.2/24"]})

    reasons = provision.clear_managed_addresses(store, run=run)

    assert not [cmd for cmd in run.cmds if "del" in cmd]
    assert reasons and "could not be read" in reasons[0]
    assert store.values["managed_segment_iface"] == "eth0.2", \
        "a record the panel could not read was rewritten anyway"


@pytest.mark.parametrize("key", provision.OWNERSHIP_KEYS)
def test_a_store_fault_on_any_ownership_scalar_still_refuses_to_narrow(key):
    """...and the cover it feeds is short, so nothing narrows off an interface the unread record may
    have been the only thing naming."""
    store = _ownership_store(read_raises=[key])

    cover = provision.superseded_state(store, "eth0.9")

    assert cover.known is False and cover.may_narrow is False
    assert "simulated settings read failure" in cover.why()


# --- ...and the FALSEY non-text values that used to be flattened before the type gate ran -------
#
# `managed_host_state` is the same three scalars read for the undo's `installed`, and it documented
# that it need not prove their shape because its one consumer decodes them through `_as_candidate`
# first. `or ""` ran BEFORE that gate and flattened every falsey non-text value — `0`, `False`, `[]`,
# `{}` — into the one string that means "the panel claims no address here". So the gate was never
# reached: an address that IS installed was left out of the orphan report and out of the survivor
# ledger, and the pending record settled over a record that had not been read.


@pytest.mark.parametrize("value", [0, False, [], {}, 0.0])
def test_a_falsey_non_text_ownership_value_reads_as_unknown_and_not_as_no_address(value):
    """Handed over unchanged, so the decode sees what the store holds and refuses the whole record —
    the undo then stays pending and the enforcement keeps covering the candidate."""
    store = _Store()
    store.values.update({"managed_segment_iface": "eth0.9",
                         "managed_segment_addr4": "192.168.10.2/24",
                         "managed_segment_addr6": value})

    installed = provision.managed_host_state(store)

    assert installed["addr6"] is value, "the value was flattened before the type gate could see it"
    with pytest.raises(provision.RecordUnknown):
        provision._as_candidate(installed)


@pytest.mark.parametrize("field", ["iface", "addr4", "addr6"])
def test_a_falsey_non_text_value_under_any_ownership_key_is_refused(field):
    """All three, because the undo reads all three and the interface is what its delete is compared
    against. `0` under the interface key coerced to `"0"`, which is a legal interface name."""
    store = _Store()
    store.values.update(dict(zip(provision.OWNERSHIP_KEYS,
                                 ["eth0.9", "192.168.10.2/24", "fd00:1:2:3::1/64"])))
    store.values[dict(zip(("iface", "addr4", "addr6"), provision.OWNERSHIP_KEYS))[field]] = 0

    with pytest.raises(provision.RecordUnknown):
        provision._as_candidate(provision.managed_host_state(store))


def test_an_unset_ownership_key_is_still_a_legitimate_blank():
    """The one value that IS mapped, because it is the store's own "no such key" — the same mapping
    `read_record` makes. A pass that provisioned nothing must keep reading as claiming nothing."""
    installed = provision.managed_host_state(_Store())

    assert installed == {"iface": "", "addr4": "", "addr6": ""}
    decoded = provision._as_candidate(installed)
    assert decoded is not None and decoded.names_candidate is False


def test_the_ownership_scalars_the_panel_writes_read_back_through_the_undo_unchanged():
    """And the shapes it does write still decode: one interface name and two canonical addresses."""
    store = _Store()
    store.values.update(dict(zip(provision.OWNERSHIP_KEYS,
                                 ["eth0.9", "192.168.10.2/24", "fd00:1:2:3::1/64"])))

    decoded = provision._as_candidate(provision.managed_host_state(store))

    assert (decoded.iface, decoded.addr4, decoded.addr6) == ("eth0.9", "192.168.10.2/24",
                                                            "fd00:1:2:3::1/64")


# --- clear: a terminal form the readers ignore, not a deletion the store may skip --------------

def test_a_clear_writes_and_proves_a_terminal_form_the_readers_ignore():
    """The blanking is the least trustworthy half of a clear, so it is not what the clear rests on:
    the terminal value is written and read back, and the blank write is best effort on top of it."""
    store = _Store(write_noop=[])
    store.set_setting(KEY, "the live record")

    def blank_only_noop(key, value):
        if key == KEY and value == "":
            return                                  # "deleted", and the old value is still there
        _Store.set_setting(store, key, value)

    store.set_setting = blank_only_noop
    assert provision.clear_record(store, KEY, terminal="settled") == ""
    assert store.values[KEY] == "settled", "the clear depended on a blanking that never landed"


def test_a_clear_whose_terminal_write_is_a_no_op_is_reported():
    """A store that keeps nothing at all cannot be worked around, only reported — which is the whole
    difference from taking a non-raising write as proof of deletion."""
    store = _Store()
    store.set_setting(KEY, "the live record")
    store.write_noop = {KEY}

    why = provision.clear_record(store, KEY, terminal="settled")

    assert why and store.values[KEY] == "the live record"


def test_a_clear_with_no_terminal_form_is_still_read_back():
    """A ledger needs no terminal form — empty already means nothing — but it still may not be
    cleared on trust."""
    store = _Store()
    store.set_setting(KEY, PAIRS[0])
    store.write_noop = {KEY}

    assert provision.clear_record(store, KEY) != ""
    store.write_noop = set()
    assert provision.clear_record(store, KEY) == ""
    assert provision.read_record(store, KEY).text == ""


# --- the COVER read through the same boundary, one state at a time -------------------------------
#
# `superseded_state` is asked three questions no other reader asks: is this pass a MOVE, what does the
# NM drop-in take over, and may the enforcement narrow back. It was reading two of its three records
# for itself — `managed_segment_iface` straight out of the record, the stale ledger straight out of
# `_parse_stale` — so a scalar the panel did not write, and one entry the boundary retains, both came
# back as "no answer at all". `_provision_segment` declines a short answer before it configures
# anything, and neither of those states is healed by a pass that never gets that far: one refused
# change with a heal behind it became provisioning refused for ever.
#
# So the source is the boundary (`_ownership_cover`), and the three states it distinguishes are three
# different answers here: unreadable is unknown, invalid names nothing and blocks nothing, and a
# retained entry contributes whatever half of it IS readable.


def _ownership(store, iface="eth0.2", addr4="192.168.9.2/24", addr6="", stale="") -> _Store:
    """A store recording what the panel owns, with the value under test swapped in."""
    store.values.update({"managed_segment_iface": iface, "managed_segment_addr4": addr4,
                         "managed_segment_addr6": addr6, provision.STALE_KEY: stale})
    return store


def test_an_invalid_interface_scalar_names_nothing_and_costs_the_answer_nothing():
    """THE FINDING. A value that is not an interface name enforces on nothing, so carrying it as a
    name is useless — and refusing over it is the wedge, because the pass that would rewrite it is the
    pass this answer gates. The valid stale pair beside it is covered, which is what used to go with
    it."""
    store = _ownership(_Store(), iface="eth0.2 eth0.9", stale="eth0.7 192.168.8.2/24")

    superseded = provision.superseded_state(store, "eth0.2")

    assert superseded.known is True, "a value the pass heals on its way past refused the pass"
    assert superseded.names == ["eth0.7"], "the readable half of the answer was discarded with it"
    assert provision.enforcement_cover(store, "eth0.2").names == ["eth0.7"]
    assert "eth0.2 eth0.9" not in superseded.names, "text no rule can match was carried as coverage"


def test_a_retained_pair_contributes_the_interface_it_names():
    """A pair is retained when EITHER half could not be read, and the interface is validated on its
    own: an entry kept for an unreadable ADDRESS still names a real interface that may be carrying a
    panel-owned address, which is the entry's whole purpose. It is covered, and it is not touched."""
    store = _ownership(_Store(), stale="eth0.7 not-an-address")

    own = provision._read_ownership(store)
    superseded = provision.superseded_state(store, "eth0.2")

    assert own.retained == (("eth0.7", "not-an-address"),) and own.stale == ()
    assert superseded.known is True and superseded.names == ["eth0.7"]
    assert superseded.may_narrow is False, "the enforcement narrowed off a retained interface"


def test_a_retained_pair_whose_interface_is_unusable_covers_nothing_and_blocks_nothing():
    """The other half of the same entry. Its interface names nothing enforceable, so there is nothing
    to cover — and it is preserved on every pass, so refusing over it would refuse them all."""
    store = _ownership(_Store(), stale="eth0.2/x 192.168.7.2/24")

    superseded = provision.superseded_state(store, "eth0.2")

    assert superseded.known is True and superseded.names == []
    assert superseded.why() == ""
    assert provision._read_ownership(store).retained == (("eth0.2/x", "192.168.7.2/24"),)


@pytest.mark.parametrize("key", [*provision.OWNERSHIP_KEYS, provision.STALE_KEY])
def test_a_record_the_store_will_not_answer_still_makes_the_cover_unknown(key):
    """The line that has to hold while the rest of this loosens: UNREADABLE is not invalid. The set of
    interfaces this would name has something already dropped out of it, so the answer is short, the
    narrowing is forbidden, and the reason travels to the caller that has to keep covering."""
    store = _ownership(_Store(read_raises=[key]), stale="eth0.7 192.168.8.2/24")

    superseded = provision.superseded_state(store, "eth0.2")
    cover = provision.enforcement_cover(store, "eth0.2")

    for state in (superseded, cover):
        assert state.known is False, "a record nobody could read answered for the whole host"
        assert state.may_narrow is False
        assert "simulated settings read failure" in state.why()


def test_the_records_the_panel_writes_are_read_exactly_as_they_were():
    """The clean paths, unchanged: the recorded interface, every stale interface and the link ledger
    all still name what they always did, and a store with nothing in it still answers KNOWN."""
    store = _ownership(_Store(), iface="eth0.2",
                       stale="eth0.2 192.168.9.2/24\neth0.7 192.168.8.2/24")
    store.values[provision.LINK_KEY] = "eth0.3"

    superseded = provision.superseded_state(store, "eth0.9")

    # Asked as a set: every source is still consulted and nothing is named twice, which is the
    # contract. The ORDER names are merged in is not one — no caller reads it.
    assert superseded.known is True and len(superseded.names) == 3
    assert set(superseded.names) == {"eth0.2", "eth0.7", "eth0.3"}
    assert set(provision.superseded_state(store, "eth0.2").names) == {"eth0.7", "eth0.3"}

    fresh = provision.superseded_state(_Store(), "eth0.2")
    assert (fresh.known, fresh.names, fresh.may_narrow) == (True, [], True)
