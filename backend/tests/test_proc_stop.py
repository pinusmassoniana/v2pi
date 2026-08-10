"""The one place that stops a child process, and the contract its three callers rely on.

Every supervisor used to hand-roll `terminate` -> bounded wait -> `kill` -> *unbounded* wait,
and the last step is what freezes the caller: SIGKILL cannot be caught, but a child wedged in
an uninterruptible syscall is not reaped by it either. So both waits are bounded here, and a
survivor is reported rather than reported as stopped.

Bounded each is not the same as bounded together: a caller running against a deadline of its
own hands over what is LEFT of it, so the two waits spend one budget instead of an allowance
apiece. `budget` defaults to `grace + reap`, which is the fixed grace the supervisors want.
"""
import logging
import subprocess

import pytest

from pi_gw_panel.proc import REAP_TIMEOUT, TERM_GRACE, stop_process


class _Clock:
    """A clock the test moves by hand — here, by however long each wait actually blocked."""

    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def spend(self, seconds: float) -> None:
        self.t += seconds


class _Proc:
    """A child that dies after `signals_ignored` signals; refuses any wait with no bound.

    With a `clock`, a wait that times out spends its own bound on it — so the test can watch
    the budget drain across both waits. `overrun` is scheduling slop charged to the FIRST wait
    only: a loaded box returns late, and the second wait has to notice.
    """

    def __init__(self, signals_ignored=0, clock=None, overrun=0.0):
        self._ignore = signals_ignored
        self._clock = clock
        self._overrun = overrun
        self.waits = []
        self.signals = []
        self.alive = True

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.signals.append("TERM")
        self._settle()

    def kill(self):
        self.signals.append("KILL")
        self._settle()

    def _settle(self):
        if self._ignore:
            self._ignore -= 1
        else:
            self.alive = False

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if timeout is None:
            raise AssertionError("wait() with no timeout — this call can block forever")
        if self.alive:
            if self._clock is not None:
                self._clock.spend(timeout + self._overrun)
                self._overrun = 0.0
            raise subprocess.TimeoutExpired("child", timeout)
        return 0

    @property
    def pid(self):
        return 77


def test_a_child_that_exits_on_sigterm_is_never_killed():
    proc = _Proc()
    assert stop_process(proc) is True
    assert proc.signals == ["TERM"]
    assert proc.waits == [TERM_GRACE]


def test_a_child_that_ignores_sigterm_is_killed_and_reaped_within_a_bound():
    proc = _Proc(signals_ignored=1)
    assert stop_process(proc) is True
    assert proc.signals == ["TERM", "KILL"]
    assert proc.waits == [TERM_GRACE, REAP_TIMEOUT]


def test_a_child_that_outlives_sigkill_is_reported_as_not_stopped(caplog):
    proc = _Proc(signals_ignored=99)
    with caplog.at_level(logging.ERROR, logger="pi_gw_panel"):
        assert stop_process(proc, name="dnsmasq") is False
    assert proc.waits == [TERM_GRACE, REAP_TIMEOUT], "a wait ran unbounded"
    assert any("NOT stopped" in r.getMessage() for r in caplog.records)


def test_nothing_to_stop_is_success():
    assert stop_process(None) is True
    already_gone = _Proc()
    already_gone.alive = False
    assert stop_process(already_gone) is True
    assert already_gone.signals == [], "signalled a child that had already exited"


def test_a_negative_budget_still_yields_a_bounded_wait():
    """Callers pass what is LEFT of a deadline, which can be past. `wait(timeout=-1)` would
    raise `ValueError` out of a `finally`; the bound has to clamp, not go negative."""
    proc = _Proc(signals_ignored=99)
    assert stop_process(proc, grace=-5.0, reap=-1.0) is False
    assert proc.waits == [0.0, 0.0]


@pytest.mark.parametrize("budget, expected", [
    (0.0, [0.0, 0.0]),          # nothing left: both waits are zero, and the child is still gone
    (1.0, [1.0, 0.0]),          # short of the grace: the reap gets what the grace did not use
    (6.0, [5.0, 1.0]),          # the full grace, then the remainder — not another full reap
    (None, [TERM_GRACE, REAP_TIMEOUT]),                    # the default total is grace + reap
])
def test_a_total_budget_is_spent_across_both_waits_not_once_each(budget, expected):
    """The two waits share one budget. Handing each its own allowance is how a caller that
    passed the remainder of *its* deadline gets charged a full reap past the end of it — the
    through-node probe passes exactly that, and the liveness worker behind it pays per stuck
    probe. So: a monotonic deadline armed on entry, and `min(cap, what is left)` per wait."""
    clock = _Clock()
    proc = _Proc(signals_ignored=99, clock=clock)
    assert stop_process(proc, budget=budget, clock=clock) is False, \
        "a survivor was reported as stopped"
    assert proc.waits == expected
    assert clock() == sum(expected) <= (budget if budget is not None else TERM_GRACE + REAP_TIMEOUT)


def test_the_default_total_holds_when_the_grace_wait_overruns():
    """`grace + reap` is what this function advertises to the supervisors that take the default.
    Two independent allowances make that a floor rather than a bound: a wait that returns late
    (a loaded box, which is when a child needs killing) adds its slop on top of a full reap."""
    clock = _Clock()
    proc = _Proc(signals_ignored=99, clock=clock, overrun=1.0)   # the grace wait costs 6 s, not 5
    assert stop_process(proc, clock=clock) is False
    assert proc.waits == [TERM_GRACE, 1.0], "the reap ignored the second the grace overran by"
    assert clock() == pytest.approx(TERM_GRACE + REAP_TIMEOUT)
