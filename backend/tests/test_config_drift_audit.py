"""A config rewritten on disk that nothing reloaded must be reported, and must fail readiness.

The supervisor records the digest of the config the running process actually loaded
(`status()["loaded_config_digest"]`). Nothing compared it: `/api/status` answered `running: true`
and `/api/ready` answered `xray: true` while the live process kept serving an older config. On the
revocation path that is the difference between "cut off" and "still admitted" — the store says the
credential is gone, the file on disk says it is gone, and the process still admits it.

These tests pin the whole channel: the comparison itself, the `/api/status` surface, the readiness
check, and — because the answer is memoized on the file's identity for polling cost — that the
memo can never hide a rewrite. "Unknown" is pinned hardest of all: it is a third answer, never
folded into "matches".
"""
import json
import os
import signal
import time

from fastapi.testclient import TestClient

from conftest import _build_dryrun_state, _login
from pi_gw_panel.app import create_app
from pi_gw_panel.net_control import netcheck
from pi_gw_panel.xray_config.validate import config_digest


def _config(tag: str, padding: int = 0) -> dict:
    """A valid-shaped config. `padding` only ever lengthens it — see `_write` for why."""
    clients = [{"id": f"{tag}-{i}"} for i in range(1 + padding)]
    return {"inbounds": [{"tag": tag, "settings": {"clients": clients}}]}


def _write(path: str, cfg: dict) -> None:
    """Write the config the way a hand-edit over SSH would: in place, same inode.

    Configs here are mostly of different lengths, which keeps each rewrite obvious to a reader
    without leaning on any one field of the memo key. The one test that deliberately writes an
    equal-length replacement, and puts the mtime back on top of it, is the regression that pins
    st_ctime_ns into that key.
    """
    with open(path, "w") as f:
        json.dump(cfg, f)


def _status(client: TestClient) -> dict:
    response = client.get("/api/status")
    assert response.status_code == 200
    return response.json()


def _ready(client: TestClient) -> tuple[int, dict]:
    response = client.get("/api/ready")
    return response.status_code, response.json()


def _start(state) -> None:
    """`start()` returns None; the truthful proof it spawned is `running`."""
    state.supervisor.start()
    assert state.supervisor.status()["running"] is True, "the stub xray did not come up"


def _app(settings, stub_xray):
    state = _build_dryrun_state(settings, stub_xray)
    client = TestClient(create_app(settings, state=state))
    _login(client)
    return state, client


def _wait_until_down(state, timeout: float = 5.0) -> None:
    """Wait for the supervisor to NOTICE a child that died on its own. Nothing tells it — the
    next `status()` is what reaps the process, so the wait is on that."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if state.supervisor.status()["running"] is False:
            return
        time.sleep(0.02)
    raise AssertionError("the stub xray was still running after SIGKILL")


# --- the finding: rewritten on disk, never reloaded ----------------------------------


def test_a_config_rewritten_without_a_reload_is_reported_as_drift_and_fails_readiness(
        settings, stub_xray):
    loaded, rewritten = _config("live"), _config("revoked", padding=3)
    assert config_digest(loaded) != config_digest(rewritten)
    _write(settings.config_path, loaded)
    state, client = _app(settings, stub_xray)
    try:
        _start(state)
        assert _status(client)["config_drift"] == "ok"

        # The revocation wrote the file; the reload threw / never happened.
        _write(settings.config_path, rewritten)

        body = _status(client)
        assert body["running"] is True, "precondition: the stale process is still up"
        assert body["config_drift"] == "drift", \
            "a config nobody reloaded was reported as the one being served"

        code, ready = _ready(client)
        assert code == 503, "a gateway serving a superseded config answered ready"
        assert ready["checks"]["xray_config"] is False
        assert ready["checks"]["xray"] is True, \
            "the process really is running — that check must keep its own meaning"
        assert "different from the configuration on disk" in ready["details"]["xray_config"]
        assert config_digest(loaded)[:12] in ready["details"]["xray_config"]
    finally:
        state.supervisor.stop()


def test_a_running_xray_on_the_config_on_disk_reports_no_drift_and_stays_ready(
        settings, stub_xray):
    _write(settings.config_path, _config("live"))
    state, client = _app(settings, stub_xray)
    try:
        _start(state)

        assert _status(client)["config_drift"] == "ok"
        _, ready = _ready(client)
        assert ready["checks"]["xray_config"] is True
        assert "xray_config" not in ready["details"], \
            "a detail is a reason for a failure, never noise on a healthy gateway"
    finally:
        state.supervisor.stop()


def test_a_stop_and_a_start_on_the_new_config_clear_the_drift(settings, stub_xray):
    """The repair has to be visible, or the operator cannot tell a fixed gateway from a broken
    one and restarts xray forever."""
    first, second = _config("first"), _config("second", padding=5)
    _write(settings.config_path, first)
    state, client = _app(settings, stub_xray)
    try:
        _start(state)
        _write(settings.config_path, second)
        assert _status(client)["config_drift"] == "drift"

        assert state.supervisor.stop() is True
        assert _status(client)["config_drift"] == "unknown", \
            "a stopped supervisor knows nothing about what is loaded — that is not a match"

        _start(state)
        assert _status(client)["config_drift"] == "ok", \
            "a restart onto the rewritten config still reported drift"
        code, ready = _ready(client)
        assert ready["checks"]["xray_config"] is True
        assert code in (200, 503)      # other layers are dry-run here; this check is green
    finally:
        state.supervisor.stop()


def test_a_restored_backup_drifts_too_and_the_detail_claims_no_chronology(settings, stub_xray):
    """The other direction: the live process holds the NEWER config and an operator restored an
    older one over the file. Two digests prove the process and the file disagree; they prove
    nothing about which came first. A detail that calls the running config "older" is wrong
    exactly here, and sends the operator hunting for a change nobody made instead of restarting
    xray onto the file they just restored.
    """
    running_cfg, restored = _config("current", padding=6), _config("backup")
    _write(settings.config_path, running_cfg)
    state, client = _app(settings, stub_xray)
    try:
        _start(state)
        assert _status(client)["config_drift"] == "ok"

        _write(settings.config_path, restored)         # the FILE is now the older side
        assert _status(client)["config_drift"] == "drift", \
            "a restored backup the live process never loaded was reported as being served"

        code, ready = _ready(client)
        assert code == 503
        # The path is a tmp dir named after this test, so judge the WORDING, not the path.
        wording = ready["details"]["xray_config"].replace(settings.config_path, "<config>")
        assert "different from the configuration on disk" in wording
        for claim in ("older", "newer", "after", "since", "rewritten"):
            assert claim not in wording, f"the detail claims chronology it cannot know: {claim!r}"
        assert config_digest(running_cfg)[:12] in wording, "the loaded digest is not named"
        assert config_digest(restored)[:12] in wording, "the on-disk digest is not named"
    finally:
        state.supervisor.stop()


# --- unknown is a third answer -------------------------------------------------------


def test_a_child_that_simply_exited_is_unknown_and_never_drift(settings, stub_xray):
    """A natural exit is not a stop, and it leaves the loaded fingerprint behind.

    Only a stop the supervisor could CONFIRM clears `_loaded_digest` (`_stop_child`) — a child
    that outlived SIGKILL is still serving what it loaded, which is precisely when the digest is
    needed. So a child that just dies (crash, OOM kill, `kill` over SSH) keeps its fingerprint
    until the watchdog restarts it, and comparing that to the file reports drift for a process
    that is gone: /api/status would answer `running: false` and `config_drift: "drift"` in one
    body, and the dashboard would raise STALE CONFIG beside an xray that is not running at all.
    """
    loaded, rewritten = _config("live"), _config("revoked", padding=3)
    _write(settings.config_path, loaded)
    state, client = _app(settings, stub_xray)
    try:
        _start(state)
        os.kill(state.supervisor.status()["pid"], signal.SIGKILL)   # NOT stop(): nobody is told
        _wait_until_down(state)
        assert state.supervisor.status()["loaded_config_digest"] is not None, \
            "precondition: a natural exit leaves the fingerprint an explicit stop clears"

        _write(settings.config_path, rewritten)
        body = _status(client)
        assert body["running"] is False
        assert body["config_drift"] == "unknown", \
            "drift was reported against the fingerprint of a process that had already exited"
        assert body["config_drift"] != "ok", "and it is still not a match"

        _, ready = _ready(client)
        assert ready["checks"]["xray_config"] is True, \
            "a dead process is not a proven divergence — that failure belongs to `xray`"
        assert ready["checks"]["xray"] is False, \
            "the check that IS about the process must still carry the failure"

        # The contrast that makes the gap concrete: a confirmed stop clears the fingerprint,
        # a natural exit (above) does not.
        assert state.supervisor.stop() is True
        assert state.supervisor.status()["loaded_config_digest"] is None
    finally:
        state.supervisor.stop()


def test_nothing_started_yet_is_unknown_and_does_not_fail_a_healthy_boot(settings, stub_xray):
    """The normal state at boot: a perfectly good config on disk and no process that has loaded
    it. It must not read as drift (nothing has diverged) and must not read as "ok" either —
    nothing has been compared. Failing closed here would pin /api/ready at 503 on every healthy
    boot and make the host migration script roll a good cutover back."""
    _write(settings.config_path, _config("live"))
    client = _app(settings, stub_xray)[1]      # nothing is started: the supervisor stays untouched

    body = _status(client)
    assert body["running"] is False
    assert body["config_drift"] == "unknown", \
        "an unstarted xray was reported as serving the config on disk"
    assert body["config_drift"] != "ok"

    _, ready = _ready(client)
    assert ready["checks"]["xray_config"] is True, \
        "a healthy boot was failed for a comparison that has not happened yet"
    assert "xray_config" not in ready["details"]


def test_a_config_that_could_not_be_parsed_at_load_is_unknown_even_once_disk_is_good_again(
        settings, stub_xray):
    """The supervisor records None when it could not parse what it was starting on. None is
    "unknown", so a later good config on disk must not turn that into a match — nobody knows
    what the live process is serving, and claiming it is current is the original bug.

    Readiness draws the line at the process: the verdict stays "unknown" (it is not a proven
    divergence), but a LIVE process whose configuration cannot be established is not a gateway
    a migration may commit on, so the check fails and says what could not be verified.
    """
    _write(settings.config_path, _config("live"))
    state, client = _app(settings, stub_xray)
    try:
        _start(state)
        with open(settings.config_path, "w") as f:
            f.write('{"inbounds": [')                 # truncated by a crash mid-write
        assert state.supervisor.reload() is True
        assert state.supervisor.status()["loaded_config_digest"] is None

        assert _status(client)["config_drift"] == "unknown"

        _write(settings.config_path, _config("repaired", padding=7))
        body = _status(client)
        assert body["running"] is True
        assert body["config_drift"] == "unknown", \
            "an unknown load was compared equal to whatever happens to be on disk"
        code, ready = _ready(client)
        assert code == 503
        assert ready["checks"]["xray"] is True, "precondition: the process really is up"
        assert ready["checks"]["xray_config"] is False, \
            "a live process nobody can name a configuration for answered ready"
        assert "could not be verified" in ready["details"]["xray_config"]
    finally:
        state.supervisor.stop()


def test_an_unreadable_config_is_unknown_and_not_drift(settings, stub_xray):
    """The file the running process loaded is gone. That is a repair to make, but it is not
    evidence of divergence, and reporting drift would send the operator to restart xray onto a
    config that is not there — so the verdict is "unknown", not "drift".

    It is still no basis to commit a migration on: the live process is serving something, and
    with the file gone there is nothing left to compare it against.
    """
    _write(settings.config_path, _config("live"))
    state, client = _app(settings, stub_xray)
    try:
        _start(state)
        os.unlink(settings.config_path)

        assert _status(client)["config_drift"] == "unknown"
        _, ready = _ready(client)
        assert ready["checks"]["xray"] is True
        assert ready["checks"]["xray_config"] is False
        assert "could not be verified" in ready["details"]["xray_config"]
    finally:
        state.supervisor.stop()


def test_an_unparseable_config_with_nothing_running_keeps_the_boot_ready(settings, stub_xray):
    """The same unreadable config, with no process that loaded it: still green.

    Nothing is serving anything, so there is no unverified live configuration here — only a
    repair to make before the first start. Failing this check would pin /api/ready at 503 on a
    boot that has not started xray yet and make the host migration script roll a good cutover
    back; the absent process is `xray`'s failure, and it is reported there, once.
    """
    _write(settings.config_path, _config("live"))
    client = _app(settings, stub_xray)[1]              # nothing is started
    with open(settings.config_path, "w") as f:
        f.write('{"inbounds": [')                      # truncated by a crash mid-write

    body = _status(client)
    assert body["running"] is False
    assert body["config_drift"] == "unknown"

    _, ready = _ready(client)
    assert ready["checks"]["xray"] is False
    assert ready["checks"]["xray_config"] is True, \
        "a boot with nothing started was failed for a config no process has loaded"
    assert "xray_config" not in ready["details"]


# --- the memo that keeps this affordable on a 3s poll --------------------------------


def test_the_memoized_disk_digest_follows_a_rewrite(settings, stub_xray):
    """`/api/status` is polled every 3s per open tab, so the on-disk digest is memoized on the
    file's (inode, mtime_ns, size). A memo that outlived a rewrite would restore the exact
    silence this whole comparison removes, so it is pinned directly: read, rewrite, read."""
    first, second = _config("first"), _config("second", padding=4)
    _write(settings.config_path, first)
    assert netcheck.disk_config_digest(settings.config_path) == config_digest(first)

    _write(settings.config_path, second)
    assert netcheck.disk_config_digest(settings.config_path) == config_digest(second), \
        "the memo served a digest of a config that had already been replaced"


def test_an_equal_length_rewrite_that_puts_the_mtime_back_is_still_seen(settings):
    """The memo may not outlive a rewrite that leaves (inode, mtime, size) untouched.

    `touch -r`, an editor or restore that preserves timestamps, rsync --times, or simply two
    writes inside one filesystem timestamp tick all produce a file with the same inode, the same
    length and the same mtime as the one already memoized. Keyed on those three the memo answers
    with the previous digest for the life of the process — `/api/status` reports "no drift"
    forever while xray serves something else, which is the exact silence this feature removes.
    st_ctime_ns closes it: every write moves it, and no `utime` moves it back.
    """
    first, second = _config("aaaa"), _config("bbbb")      # same length on purpose
    assert config_digest(first) != config_digest(second)
    _write(settings.config_path, first)
    before = os.stat(settings.config_path)
    assert netcheck.disk_config_digest(settings.config_path) == config_digest(first)

    time.sleep(0.01)                                      # land the rewrite in a later tick...
    _write(settings.config_path, second)
    os.utime(settings.config_path, ns=(before.st_atime_ns, before.st_mtime_ns))   # ...mtime back
    after = os.stat(settings.config_path)
    assert (after.st_ino, after.st_mtime_ns, after.st_size) == \
           (before.st_ino, before.st_mtime_ns, before.st_size), \
        "precondition: (inode, mtime, size) alone cannot tell these two files apart"

    assert netcheck.disk_config_digest(settings.config_path) == config_digest(second), \
        "the memo kept serving the digest of a config that had already been replaced"


def test_the_memo_is_keyed_to_the_file_whose_bytes_it_hashed(settings, monkeypatch):
    """`stat(path)` and then `open(path)` are two lookups that can land on two files.

    A replace slipped into that window files the digest of one file under the identity of
    another, and that mapping is served back for as long as the identity is at the path. Taking
    the identity with `fstat` off the descriptor the bytes come from leaves no window at all —
    so the swap wired below never fires — but the invariant asserted at the end holds either
    way: whatever the memo recorded describes the file the recorded digest came from.
    """
    first, second = _config("first"), _config("second", padding=2)
    _write(settings.config_path, first)
    replacement = settings.config_path + ".incoming"
    _write(replacement, second)
    real_stat, swapped = os.stat, []

    def stat_then_swap(*args, **kwargs):
        info = real_stat(*args, **kwargs)
        if args and args[0] == settings.config_path and not swapped:
            swapped.append(True)
            os.replace(replacement, settings.config_path)   # the window, taken
        return info

    monkeypatch.setattr(netcheck.os, "stat", stat_then_swap)
    digest = netcheck.disk_config_digest(settings.config_path)
    monkeypatch.undo()

    with open(settings.config_path) as f:
        now = os.fstat(f.fileno())            # reading does not move any field of the key
        on_disk = json.load(f)
    path, key, memoized = netcheck._disk_digest_memo
    assert (path, memoized) == (settings.config_path, digest)
    assert key == (now.st_dev, now.st_ino, now.st_mtime_ns, now.st_ctime_ns, now.st_size), \
        "the memo recorded the identity of a file whose bytes it never hashed"
    assert digest == config_digest(on_disk)


def test_the_digest_of_a_missing_file_is_unknown_and_not_the_last_one_seen(settings):
    _write(settings.config_path, _config("live"))
    assert netcheck.disk_config_digest(settings.config_path) is not None
    os.unlink(settings.config_path)
    assert netcheck.disk_config_digest(settings.config_path) is None
    assert netcheck.disk_config_digest("") is None


# --- the comparison in isolation -----------------------------------------------------


class _Supervisor:
    """The only three things `config_drift` reads off a supervisor."""

    def __init__(self, digest, config_path, running=True):
        self._digest, self.config_path, self._running = digest, config_path, running

    def status(self):
        return {"running": self._running, "loaded_config_digest": self._digest}


def test_config_drift_reads_the_path_off_the_supervisor_and_compares_digest_to_digest(
        settings):
    cfg = _config("live")
    _write(settings.config_path, cfg)

    assert netcheck.config_drift(
        _Supervisor(config_digest(cfg), settings.config_path))[0] == "ok"
    assert netcheck.config_drift(
        _Supervisor(config_digest(_config("other", padding=2)), settings.config_path))[0] == "drift"
    # An unknown load, with a readable config sitting right there: still unknown. This is the
    # assertion that fails the moment None is made to compare equal to anything.
    assert netcheck.config_drift(_Supervisor(None, settings.config_path))[0] == "unknown"
    # A digest left behind by a process that is no longer alive answers about nothing.
    assert netcheck.config_drift(
        _Supervisor(config_digest(_config("gone", padding=1)), settings.config_path,
                    running=False))[0] == "unknown"


def test_config_drift_survives_a_supervisor_that_cannot_answer(settings):
    class _Broken:
        config_path = settings.config_path

        def status(self):
            raise RuntimeError("supervisor lock is wedged")

    _write(settings.config_path, _config("live"))
    assert netcheck.config_drift(_Broken()) == ("unknown", ""), \
        "a probe that could not run must not take /api/status or /api/ready down with it"
