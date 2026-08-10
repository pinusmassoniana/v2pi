"""Stopping a child process inside a budget — the one implementation of it.

The panel owns three long-lived children (dnsmasq, the DHCPv6-PD `dhclient`, the throwaway
xray a probe spins up) and every supervisor wrote the same sequence by hand:

    terminate() -> wait(timeout=grace) -> kill() -> wait()

The last step is the bug, and it was written three times because it was written three times.
`SIGKILL` cannot be caught, so the second wait "obviously" returns at once — except when the
child is stuck in an uninterruptible syscall (`D` state: a wedged netlink or filesystem call is
exactly what makes a gateway process unkillable in the first place), or when it is not really
ours to reap. Then `wait()` blocks forever, holding whatever the caller held: the apply-lock
during provisioning, or the health worker that drives the xray watchdog and auto-failover.

So: both waits are bounded, and a child that outlives both is *reported*, never reported as
stopped. Callers decide what a survivor means — the supervisors keep the handle and raise, so
nothing spawns a second child next to the first; the throwaway probe logs and moves on — but
nobody gets to block, and nobody gets to pretend.

Bounded is not the same as cheap, though: two independent allowances add up. A caller that is
itself running against a deadline hands over what is LEFT of it, and it must get back a stop
that costs no more than that — so the two waits share ONE budget here rather than drawing an
allowance each.
"""
import logging
import subprocess
import time

_log = logging.getLogger("pi_gw_panel")

TERM_GRACE = 5.0    # SIGTERM grace: dnsmasq/dhclient exit in milliseconds when they are healthy
REAP_TIMEOUT = 2.0  # after SIGKILL: only an uninterruptible or unreapable child needs longer


def stop_process(proc, *, grace: float = TERM_GRACE, reap: float = REAP_TIMEOUT,
                 budget: float | None = None, name: str = "child",
                 clock=time.monotonic) -> bool:
    """Stop `proc` inside ONE budget. True when the child is gone.

    ``budget`` is the total wall clock this call may spend across *both* waits. It defaults to
    ``grace + reap`` — what a caller with a fixed grace (the dnsmasq and dhclient supervisors)
    wants, and what this function has always advertised — and a deadline is armed from it on
    entry, so each wait gets ``min(its own cap, what is left)`` and never a fresh allowance.

    That distinction is the whole point for a deadline-driven caller. The through-node probe
    passes the remainder of the one deadline it promises its callers; granting ``reap`` on top
    of an exhausted budget would hand the reap its full second *precisely* when there was no
    time left to give it, pushing the liveness worker — and the watchdog and failover behind
    it — past the bound the probe exists to enforce.

    False — never an exception, and never silence — when it survived `SIGKILL`: the caller still
    has a live child and has to decide what that means, which it cannot do if this returns the
    same thing as success. A spent budget shortens the waits; it never turns a survivor into a
    success. `None` (nothing to stop) and an already-exited child are True.
    """
    if proc is None or proc.poll() is not None:
        return True
    deadline = clock() + (grace + reap if budget is None else budget)
    proc.terminate()
    try:
        proc.wait(timeout=max(0.0, min(grace, deadline - clock())))
        return True
    except subprocess.TimeoutExpired:
        pass
    proc.kill()
    left = max(0.0, min(reap, deadline - clock()))
    try:
        proc.wait(timeout=left)
        return True
    except subprocess.TimeoutExpired:
        _log.error("%s (pid %s) is still running %ss after SIGKILL; it was NOT stopped",
                   name, getattr(proc, "pid", "?"), left)
        return False
