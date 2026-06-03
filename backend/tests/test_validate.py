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
