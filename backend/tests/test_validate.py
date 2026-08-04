import json
import os
from pi_gw_panel.config import Settings
from pi_gw_panel.xray_config.validate import validate_config, ConfigManager


def test_validate_ok_with_stub(settings, stub_xray):
    ok, out = validate_config({"a": 1}, stub_xray)
    assert ok is True
    assert "OK" in out


def test_validate_fail_with_stub(settings, stub_xray, monkeypatch):
    monkeypatch.setenv("STUB_XRAY_FAIL", "1")
    ok, out = validate_config({"a": 1}, stub_xray)
    assert ok is False
    assert "error" in out.lower()


def test_apply_snapshots_previous_config_for_undo(settings, stub_xray):
    mgr = ConfigManager(settings, xray_bin=stub_xray)
    mgr.apply({"v": "one"})                              # first apply: no previous → no undo target
    assert not os.path.exists(settings.lastgood_path)
    mgr.apply({"v": "two"})                               # second apply snapshots "one"
    assert json.load(open(settings.config_path)) == {"v": "two"}
    assert json.load(open(settings.lastgood_path)) == {"v": "one"}


def test_failed_validate_touches_nothing(settings, stub_xray, monkeypatch):
    mgr = ConfigManager(settings, xray_bin=stub_xray)
    mgr.apply({"v": "one"})
    mgr.apply({"v": "two"})                               # config=two, undo target=one
    monkeypatch.setenv("STUB_XRAY_FAIL", "1")
    ok, _ = mgr.apply({"v": "bad"})
    assert ok is False
    assert json.load(open(settings.config_path)) == {"v": "two"}     # live config untouched
    assert json.load(open(settings.lastgood_path)) == {"v": "one"}   # undo target untouched


def test_rollback_reverts_to_previous_apply(settings, stub_xray):
    mgr = ConfigManager(settings, xray_bin=stub_xray)
    mgr.apply({"v": "one"})
    mgr.apply({"v": "two"})
    assert mgr.rollback() is True
    assert json.load(open(settings.config_path)) == {"v": "one"}     # reverted to the previous apply


def test_apply_creates_missing_data_dir(tmp_path, stub_xray):
    sub = tmp_path / "nested" / "data"  # does not exist yet
    s = Settings(
        data_dir=str(sub),
        db_path=str(sub / "db.sqlite"),
        config_path=str(sub / "xray.json"),
        lastgood_path=str(sub / "xray.lastgood.json"),
    )
    ok, _ = ConfigManager(s, xray_bin=stub_xray).apply({"v": "good"})
    assert ok is True
    assert os.path.exists(s.config_path)


def test_apply_replaces_config_atomically(settings, stub_xray):
    mgr = ConfigManager(settings, xray_bin=stub_xray)
    mgr.apply({"first": 1})
    mgr.apply({"second": 2})
    with open(settings.config_path) as f:
        assert json.load(f) == {"second": 2}  # fully replaced, not merged


def test_rollback_refuses_corrupt_lastgood(settings, stub_xray):
    mgr = ConfigManager(settings, xray_bin=stub_xray)
    mgr.apply({"v": "good"})
    with open(settings.lastgood_path, "w") as f:
        f.write("{ not valid json")
    assert mgr.rollback() is False


# --- rollback provenance: the snapshot must be the config the live one replaced -----------

def _damage(path: str) -> None:
    with open(path, "w") as f:
        f.write('{"outbounds": [')          # power cut mid-write / hand-edited over SSH


def test_rollback_refuses_a_snapshot_that_is_not_what_the_live_config_replaced(settings, stub_xray):
    """A → B → (B damaged on disk) → C.

    Keeping the A snapshot is right: it is the only intact artifact left, and a corrupt live
    config must not wedge every future apply. Calling it the ROLLBACK TARGET is not — A is not
    what C replaced. Callers roll back believing they are restoring the node they were just on
    (controller reloads it as `previous_node`), so promoting A there reinstates an older config
    wholesale: road-warrior clients revoked since, routing rules a later apply had fixed."""
    mgr = ConfigManager(settings, xray_bin=stub_xray)
    mgr.apply({"v": "A"})
    mgr.apply({"v": "B"})
    _damage(settings.config_path)

    assert mgr.apply({"v": "C"})[0] is True                          # still applies (F4-09)
    assert json.load(open(settings.lastgood_path)) == {"v": "A"}     # artifact kept for repair
    assert mgr.rollback_available() is False
    assert mgr.rollback() is False                                   # but never auto-promoted
    assert json.load(open(settings.config_path)) == {"v": "C"}       # live left alone


def test_rollback_provenance_returns_on_the_next_readable_apply(settings, stub_xray):
    """Failing closed must be self-healing, not a permanently dead rollback button."""
    mgr = ConfigManager(settings, xray_bin=stub_xray)
    mgr.apply({"v": "A"})
    mgr.apply({"v": "B"})
    _damage(settings.config_path)
    mgr.apply({"v": "C"})
    assert mgr.rollback() is False

    assert mgr.apply({"v": "D"})[0] is True                          # C was readable this time
    assert json.load(open(settings.lastgood_path)) == {"v": "C"}
    assert mgr.rollback() is True
    assert json.load(open(settings.config_path)) == {"v": "C"}       # exactly what D replaced


def test_rollback_refuses_when_the_live_config_was_replaced_out_of_band(settings, stub_xray):
    """Same class, different cause: whatever is live now was not written by the apply that
    produced the snapshot, so the snapshot is not its undo."""
    mgr = ConfigManager(settings, xray_bin=stub_xray)
    mgr.apply({"v": "A"})
    mgr.apply({"v": "B"})
    with open(settings.config_path, "w") as f:
        json.dump({"v": "hand-edited"}, f)

    assert mgr.rollback() is False
    assert json.load(open(settings.config_path)) == {"v": "hand-edited"}


def test_rollback_refuses_when_the_provenance_marker_is_missing(settings, stub_xray):
    """Unknown provenance is not proven provenance — including the one-off case of upgrading
    onto a data dir whose last-good predates the marker. The next apply re-establishes it."""
    mgr = ConfigManager(settings, xray_bin=stub_xray)
    mgr.apply({"v": "A"})
    mgr.apply({"v": "B"})
    os.unlink(settings.lastgood_path + ".provenance")

    assert mgr.rollback() is False

    assert mgr.apply({"v": "C"})[0] is True
    assert mgr.rollback() is True
    assert json.load(open(settings.config_path)) == {"v": "B"}


def test_rollback_stays_a_provable_no_op_when_repeated(settings, stub_xray):
    """The API exposes Rollback as a button; pressing it twice must not become an unprovable
    promotion, nor start refusing for a reason the operator cannot see."""
    mgr = ConfigManager(settings, xray_bin=stub_xray)
    mgr.apply({"v": "A"})
    mgr.apply({"v": "B"})
    assert mgr.rollback() is True
    assert mgr.rollback() is True
    assert json.load(open(settings.config_path)) == {"v": "A"}


def test_provenance_marker_is_owner_only(settings, stub_xray):
    mgr = ConfigManager(settings, xray_bin=stub_xray)
    mgr.apply({"v": "A"})
    mgr.apply({"v": "B"})
    assert os.stat(settings.lastgood_path + ".provenance").st_mode & 0o777 == 0o600


# --- an apply whose effect a rollback may never undo --------------------------------------
#
# `apply()` files the config it REPLACES as the undo target and marks the pairing valid, so
# writing a revocation through it necessarily publishes a promotable pre-revocation config —
# one button away from reinstating the credential the operator was just told was cut off.
# Invalidating the marker afterwards leaves the door open for exactly as long as that can fail,
# and after a successful revocation the marker matches the clean live config and the
# pre-revocation snapshot EXACTLY, so a swallowed error there is a promotable target rather
# than the harmless stale pairing the digest check would reject anyway.


def test_an_irreversible_apply_never_publishes_a_rollback_target(settings, stub_xray):
    mgr = ConfigManager(settings, xray_bin=stub_xray)
    mgr.apply({"v": "A"})
    mgr.apply({"v": "B"})
    assert mgr.rollback_available() is True

    assert mgr.apply_irreversible({"v": "C"})[0] is True
    assert json.load(open(settings.config_path)) == {"v": "C"}    # the write itself landed
    assert mgr.rollback_available() is False
    assert mgr.rollback() is False
    assert json.load(open(settings.config_path)) == {"v": "C"}    # ...and stayed
    assert json.load(open(settings.lastgood_path)) == {"v": "A"}  # artifact kept, never rewritten

    # self-healing, exactly as a failed snapshot is: the next ordinary apply pairs again
    assert mgr.apply({"v": "D"})[0] is True
    assert mgr.rollback() is True
    assert json.load(open(settings.config_path)) == {"v": "C"}


def test_an_interrupted_irreversible_apply_leaves_nothing_promotable(settings, stub_xray,
                                                                     monkeypatch):
    """Ordering is the whole crash argument. Invalidate-then-write can only be interrupted into
    'old config, no promotable target' — the apply simply did not happen. Write-then-invalidate
    has a window that leaves 'new config, promotable PRE-revocation target', which is the leak
    with the operator already told the device was cut off. Simulated at the write, not by
    actually crashing."""
    mgr = ConfigManager(settings, xray_bin=stub_xray)
    mgr.apply({"v": "A"})
    mgr.apply({"v": "B"})
    assert mgr.rollback_available() is True

    real = ConfigManager._write_atomic

    def _dies_writing_the_live_config(path, cfg):
        if path == settings.config_path:
            raise OSError("power cut between the invalidation and the write")
        return real(path, cfg)

    monkeypatch.setattr(ConfigManager, "_write_atomic",
                        staticmethod(_dies_writing_the_live_config))
    try:
        mgr.apply_irreversible({"v": "C"})
    except OSError:
        pass
    monkeypatch.undo()

    assert json.load(open(settings.config_path)) == {"v": "B"}    # the write never landed
    assert mgr.rollback_available() is False, \
        "a crash mid-apply left the pre-revocation config promotable"
    assert mgr.rollback() is False


def test_an_irreversible_apply_refuses_when_it_cannot_invalidate(settings, stub_xray,
                                                                 monkeypatch):
    """Fails closed at the invalidation, BEFORE the live config is touched: the caller sees a
    failed revocation (and stops xray) rather than a completed one a rollback can reverse."""
    mgr = ConfigManager(settings, xray_bin=stub_xray)
    mgr.apply({"v": "A"})
    mgr.apply({"v": "B"})

    monkeypatch.setattr(ConfigManager, "_invalidate_provenance_durably", lambda self: False)
    ok, out = mgr.apply_irreversible({"v": "C"})
    monkeypatch.undo()

    assert ok is False and "durably invalidated" in out
    assert json.load(open(settings.config_path)) == {"v": "B"}    # untouched


def test_asking_whether_a_rollback_is_available_is_quiet_but_an_attempt_says_why(settings,
                                                                                 stub_xray,
                                                                                 caplog):
    """`rollback_available()` is what `/api/status` reads, and /api/status is polled every few
    seconds. The post-revocation marker is an EXPECTED state, deliberately and permanently set,
    so warning on the CHECK emitted a warning per poll forever — noise that buries the entries
    an operator actually needs. Reserved for an ATTEMPT, where somebody wanted the answer."""
    import logging

    mgr = ConfigManager(settings, xray_bin=stub_xray)
    mgr.apply({"v": "A"})
    mgr.apply({"v": "B"})
    assert mgr.invalidate_rollback() is True

    def _warnings() -> list[str]:
        return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]

    with caplog.at_level(logging.DEBUG, logger="pi_gw_panel.xray_config.validate"):
        for _ in range(5):
            assert mgr.rollback_available() is False
        assert _warnings() == [], "an availability check warns about an expected state"
        # the reason is not lost, just demoted to where a reader has to ask for it
        assert any("deliberately invalidated" in r.getMessage() for r in caplog.records)

        assert mgr.rollback() is False
        assert any("deliberately invalidated" in m for m in _warnings()), \
            "a refused rollback attempt no longer reports why"


def test_invalidating_the_rollback_reports_whether_it_worked_and_is_durable(settings, stub_xray):
    """The answer is load-bearing for the paths that reach the live config through the ordinary
    apply, so it may not be swallowed — and the marker must be neutralised in a way that
    survives a power cut, i.e. through the fsynced atomic write rather than a bare unlink."""
    mgr = ConfigManager(settings, xray_bin=stub_xray)
    mgr.apply({"v": "A"})
    mgr.apply({"v": "B"})

    assert mgr.invalidate_rollback() is True
    marker = settings.lastgood_path + ".provenance"
    assert json.load(open(marker))["invalidated"] is True
    assert os.stat(marker).st_mode & 0o777 == 0o600
    assert mgr.rollback_available() is False
    assert mgr.rollback() is False
    assert json.load(open(settings.config_path)) == {"v": "B"}
