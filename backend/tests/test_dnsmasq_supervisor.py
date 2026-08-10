import subprocess

import pytest

from pi_gw_panel.net_control.dnsmasq_supervisor import DnsmasqSupervisor


class FakeProc:
    def __init__(self):
        self._alive = True
        self.terminated = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        self._alive = False

    def kill(self):
        self._alive = False

    @property
    def pid(self):
        return 4242


def _sup(tmp_path, procs):
    spawned = []

    def popen(cmd):
        p = FakeProc()
        spawned.append((cmd, p))
        procs.append(p)
        return p

    conf = str(tmp_path / "dnsmasq.conf")
    run = lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, "", "")
    return DnsmasqSupervisor(
        "dnsmasq", conf, popen=popen, run=run, sleep=lambda _: None), spawned, conf


def test_apply_writes_conf_and_starts(tmp_path):
    procs = []
    sup, spawned, conf = _sup(tmp_path, procs)
    sup.apply("interface=eth0.2\n")
    assert open(conf).read() == "interface=eth0.2\n"
    assert sup.status()["running"] is True
    assert spawned[0][0][:1] == ["dnsmasq"]
    assert "--conf-file=" + conf in " ".join(spawned[0][0])


def test_apply_restarts_only_on_change(tmp_path):
    procs = []
    sup, spawned, _ = _sup(tmp_path, procs)
    sup.apply("a\n")
    sup.apply("a\n")               # identical -> no restart
    assert len(spawned) == 1
    sup.apply("b\n")               # changed -> restart
    assert len(spawned) == 2
    assert procs[0].terminated is True


def test_apply_restarts_if_proc_died(tmp_path):
    procs = []
    sup, spawned, _ = _sup(tmp_path, procs)
    sup.apply("a\n")
    procs[0]._alive = False         # process crashed
    sup.apply("a\n")                # same text, but dead -> respawn
    assert len(spawned) == 2


def test_stop_terminates(tmp_path):
    procs = []
    sup, _, _ = _sup(tmp_path, procs)
    sup.apply("a\n")
    sup.stop()
    assert sup.status()["running"] is False
    assert procs[0].terminated is True


def test_invalid_candidate_keeps_previous_config_and_child(tmp_path):
    procs = []
    sup, spawned, conf = _sup(tmp_path, procs)
    sup.apply("good\n")
    previous = procs[0]

    def reject(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="bad range")

    sup._run = reject
    with pytest.raises(RuntimeError, match="bad range"):
        sup.apply("bad\n")
    assert open(conf).read() == "good\n"
    assert previous.terminated is False
    assert len(spawned) == 1


def test_dead_candidate_restores_previous_config_and_process(tmp_path):
    procs = []
    sup, spawned, conf = _sup(tmp_path, procs)
    sup.apply("good\n")

    class DeadProc(FakeProc):
        def poll(self):
            return 1

    sup._popen = lambda cmd: (spawned.append((cmd, DeadProc())), spawned[-1][1])[1]
    with pytest.raises(RuntimeError, match="exited during readiness"):
        sup.apply("candidate\n")
    assert open(conf).read() == "good\n"
    assert "good\n" == sup._last_text


# --- a dnsmasq that ignores both signals is reported, not waited on forever -----------------
#
# `stop()` ran `terminate()` -> `wait(timeout=5)` -> `kill()` -> `wait()`. That last wait had no
# bound, and every caller holds the apply-lock (and the DB lock behind it) while provisioning,
# so an unkillable child stalled every DB-touching request in the process instead of failing
# one apply. SIGKILL cannot be caught, but a child wedged in an uninterruptible syscall still
# will not be reaped — which is exactly the state a stuck gateway process ends up in.


class StuckProc(FakeProc):
    """A child that survives SIGTERM and SIGKILL alike, and refuses any unbounded wait."""

    def __init__(self):
        super().__init__()
        self.waits = []
        self.killed = False

    def poll(self):
        return None                                   # never exits

    def terminate(self):
        self.terminated = True                        # …and ignores it

    def kill(self):
        self.killed = True                            # …and this too

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if timeout is None:
            raise AssertionError(
                "wait() with no timeout — on a real unkillable child this never returns, and "
                "it is holding the apply-lock")
        raise subprocess.TimeoutExpired("dnsmasq", timeout)


def _sup_with_stuck(tmp_path):
    procs = []
    sup, spawned, conf = _sup(tmp_path, procs)
    sup._popen = lambda cmd: (spawned.append((cmd, StuckProc())), spawned[-1][1])[1]
    return sup, spawned, conf


def test_a_dnsmasq_that_survives_sigkill_is_reported_and_not_forgotten(tmp_path):
    sup, spawned, _ = _sup_with_stuck(tmp_path)
    sup.apply("a\n")
    stuck = spawned[0][1]
    with pytest.raises(RuntimeError, match="did not stop"):
        sup.stop()
    assert stuck.terminated is True and stuck.killed is True
    assert all(t is not None for t in stuck.waits), "a wait ran unbounded"
    assert len(stuck.waits) == 2, "the post-kill reap was skipped, not bounded"
    # the handle is kept: status must not claim a dnsmasq that is still serving DHCP is gone
    assert sup.status() == {"running": True, "pid": 4242}


def test_a_dnsmasq_that_cannot_be_stopped_blocks_the_apply_instead_of_doubling_it(tmp_path):
    """Two dnsmasq processes on one segment is worse than a failed apply: the second cannot bind
    the DHCP/DNS sockets, and whichever wins is not the config we just validated."""
    sup, spawned, conf = _sup_with_stuck(tmp_path)
    sup.apply("a\n")
    with pytest.raises(RuntimeError, match="did not stop"):
        sup.apply("b\n")
    assert len(spawned) == 1, "a second dnsmasq was spawned next to the one still running"
    with open(conf) as fh:
        assert fh.read() == "a\n", "the config the surviving child is not running was installed"


def test_a_rollback_still_restores_the_config_when_the_candidate_will_not_die(tmp_path):
    """The rollback path calls `stop()` too, and that call can now fail. It must not take the
    config restore down with it — and it must not start the previous dnsmasq onto sockets a
    surviving candidate still holds."""
    procs = []
    sup, spawned, conf = _sup(tmp_path, procs)
    sup.apply("good\n")
    sup._popen = lambda cmd: (spawned.append((cmd, StuckProc())), spawned[-1][1])[1]

    def boom(_seconds):
        raise RuntimeError("readiness check blew up")

    sup._sleep = boom
    with pytest.raises(RuntimeError) as caught:
        sup.apply("candidate\n")
    assert "readiness check blew up" in str(caught.value)
    assert "did not stop" in str(caught.value), "the failed stop was swallowed by the rollback"
    with open(conf) as fh:
        assert fh.read() == "good\n", "the rollback skipped the config restore"
    assert len(spawned) == 2, "the previous dnsmasq was restarted next to the surviving one"
    assert sup._last_text is None, "claimed to know what the surviving child is serving"
