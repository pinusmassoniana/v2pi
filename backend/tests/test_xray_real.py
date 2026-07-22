import json
import os
import shutil

import pytest

from pi_gw_panel.config import Settings
from pi_gw_panel.models import Node
from pi_gw_panel.xray_config.builder import build_config
from pi_gw_panel.xray_config.validate import ConfigManager, validate_config


def test_lastgood_snapshot_is_owner_only(settings, stub_xray):
    manager = ConfigManager(settings, stub_xray)
    assert manager.apply({"id": "first-secret"})[0] is True
    assert manager.apply({"id": "second-secret"})[0] is True

    assert os.stat(settings.lastgood_path).st_mode & 0o777 == 0o600


def test_rollback_does_not_partially_replace_live_config(settings, stub_xray, monkeypatch):
    manager = ConfigManager(settings, stub_xray)
    assert manager.apply({"marker": "first"})[0] is True
    assert manager.apply({"marker": "second"})[0] is True

    def interrupted_replace(_source, _target):
        raise OSError("interrupted")

    monkeypatch.setattr("pi_gw_panel.xray_config.validate.os.replace", interrupted_replace)
    with pytest.raises(OSError, match="interrupted"):
        manager.rollback()
    with open(settings.config_path) as f:
        assert json.load(f) == {"marker": "second"}


def test_validate_missing_binary_returns_short_sanitized_error(tmp_path):
    secret = "550e8400-e29b-41d4-a716-446655440000"

    ok, error = validate_config({"id": secret}, str(tmp_path / "missing-xray"))

    assert ok is False
    assert "not found" in error.lower()
    assert secret not in error


@pytest.mark.skipif(shutil.which("xray") is None, reason="real xray binary not installed")
def test_build_config_passes_real_xray_test():
    """Wave 0 stubs xray; this runs a real `xray -test` against the generated
    config wherever an xray binary is present (e.g. on the Pi / a dev box), so the
    tproxy + VLESS/Reality/Vision + DoH schema is validated end to end — closing the
    gap that the stubbed unit tests cannot cover. Skips automatically when xray
    is absent (e.g. macOS dev without it installed)."""
    node = Node(
        id=1, name="real", address="1.2.3.4", port=47000,
        uuid="00000000-0000-0000-0000-000000000000",
        sni="www.microsoft.com",
        public_key="jNXHt1yRo0vDuchQlIP6Z0ZvjT3KtzVI_T4E7RoLJS0",  # x25519 base64url shape
        short_id="0123abcd",
    )
    ok, out = validate_config(build_config(node, Settings()), "xray")
    assert ok, out
