import shutil

import pytest

from pi_gw_panel.config import Settings
from pi_gw_panel.models import Node
from pi_gw_panel.xray_config.builder import build_config
from pi_gw_panel.xray_config.validate import validate_config


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
