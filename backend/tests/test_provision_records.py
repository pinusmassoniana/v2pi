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
    unreadable, *_ = provision._read_ownership(store)
    assert unreadable == [], "only the ledger that is malformed may be reported unreadable"


def test_a_truncated_stale_entry_stops_the_pass_installing_over_the_record_it_cannot_read():
    """The ownership read is the one that decides whether addresses go on at all, and the ledger it
    would rewrite is computed from this parse: an entry that did not parse is an address on the host
    whose last record this pass would drop. So it reports, quoting the entry, and installs nothing."""
    store = _Store()
    store.set_setting(provision.STALE_KEY, "eth0.2 192.168.9.2/24\neth0.7\n")

    unreadable, iface, addr4, addr6, stale = provision._read_ownership(store)

    assert unreadable and "eth0.7" in unreadable[0]
    assert (iface, addr4, addr6, stale) == ("", "", "", [])


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
    unreadable, iface, addr4, _addr6, _stale = provision._read_ownership(store)
    assert (unreadable, iface, addr4) == ([], "eth0.2", "192.168.9.2/24")


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
    unreadable, iface, addr4, addr6, stale = provision._read_ownership(store)
    assert unreadable == [] and (iface, addr4, addr6) == ("eth0.9", "192.168.10.2/24",
                                                         "fd00:1:2:3::1/64")
    assert stale == [("eth0.2", "192.168.9.2/24")]


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
