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

    The on-disk digest is memoized on (inode, mtime_ns, size). In-place writes keep the inode, and
    two writes microseconds apart can land on the same mtime_ns on a coarse clock, so every config
    these tests write is a DIFFERENT length — that keeps the key distinct on its own and the test
    deterministic rather than dependent on filesystem timestamp resolution.
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
        assert "rewritten" in ready["details"]["xray_config"]
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


# --- unknown is a third answer -------------------------------------------------------


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
    what the live process is serving, and claiming it is current is the original bug."""
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
        _, ready = _ready(client)
        assert ready["checks"]["xray_config"] is True   # unknown never fails a boot
    finally:
        state.supervisor.stop()


def test_an_unreadable_config_is_unknown_and_not_drift(settings, stub_xray):
    """The file the running process loaded is gone. That is a repair to make, but it is not
    evidence of divergence, and reporting drift would send the operator to restart xray onto a
    config that is not there."""
    _write(settings.config_path, _config("live"))
    state, client = _app(settings, stub_xray)
    try:
        _start(state)
        os.unlink(settings.config_path)

        assert _status(client)["config_drift"] == "unknown"
        _, ready = _ready(client)
        assert ready["checks"]["xray_config"] is True
    finally:
        state.supervisor.stop()


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


def test_the_digest_of_a_missing_file_is_unknown_and_not_the_last_one_seen(settings):
    _write(settings.config_path, _config("live"))
    assert netcheck.disk_config_digest(settings.config_path) is not None
    os.unlink(settings.config_path)
    assert netcheck.disk_config_digest(settings.config_path) is None
    assert netcheck.disk_config_digest("") is None


# --- the comparison in isolation -----------------------------------------------------


class _Supervisor:
    """The only two things `config_drift` reads off a supervisor."""

    def __init__(self, digest, config_path):
        self._digest, self.config_path = digest, config_path

    def status(self):
        return {"running": True, "loaded_config_digest": self._digest}


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


def test_config_drift_survives_a_supervisor_that_cannot_answer(settings):
    class _Broken:
        config_path = settings.config_path

        def status(self):
            raise RuntimeError("supervisor lock is wedged")

    _write(settings.config_path, _config("live"))
    assert netcheck.config_drift(_Broken()) == ("unknown", ""), \
        "a probe that could not run must not take /api/status or /api/ready down with it"
