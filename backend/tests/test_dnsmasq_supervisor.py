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
