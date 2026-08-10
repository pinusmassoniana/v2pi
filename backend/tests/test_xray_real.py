import base64
import json
import os
import shutil

import pytest

from pi_gw_panel.config import Settings
from pi_gw_panel.models import Node
from pi_gw_panel.xray_config.builder import build_config
from pi_gw_panel.xray_config.validate import ConfigManager, validate_config


def _ramp(first: int) -> bytearray:
    """32 sequential bytes starting at `first` — a key-shaped value that is fake on sight."""
    return bytearray(range(first, first + 32))


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(bytes(raw)).decode().rstrip("=")


def _clamped(raw: bytearray) -> bytearray:
    """The three x25519 scalar clamp operations, so the value is a structurally valid key."""
    raw[0] &= 248
    raw[31] &= 127
    raw[31] |= 64
    return raw


# Real REALITY key material is indistinguishable from any other 43-char base64url literal, so a
# live key pasted into a fixture reads as a placeholder and survives review. These are built from
# a byte ramp instead: still exactly what xray demands of the wire format (32 raw bytes, and for
# the private scalar the clamp), but provably synthetic to a reader or a scanner. The shape is
# asserted below, because every test that consumes them skips without a real xray binary.
_RW_PRIV = _b64u(_clamped(_ramp(0x00)))
_NODE_PUB = _b64u(_ramp(0x20))


def test_reality_key_fixtures_are_synthetic_and_correctly_clamped():
    priv = base64.urlsafe_b64decode(_RW_PRIV + "==")
    assert len(priv) == 32 and len(_RW_PRIV) == 43
    assert priv[0] == (priv[0] & 248)                      # low three bits cleared
    assert priv[31] == ((priv[31] & 127) | 64)             # high bit cleared, bit 6 set
    assert priv[1:31] == bytes(range(0x01, 0x1F))          # a ramp, not entropy
    assert base64.urlsafe_b64decode(_NODE_PUB + "==") == bytes(range(0x20, 0x40))


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
        public_key=_NODE_PUB,
        short_id="0123abcd",
    )
    ok, out = validate_config(build_config(node, Settings()), "xray")
    assert ok, out


def _rw(**over):
    rw = {"port": 8443, "dest": "www.microsoft.com:443",
          "server_names": ["www.microsoft.com"], "private_key": _RW_PRIV,
          "short_ids": ["ab12cd34"],
          "clients": [{"id": "00000000-0000-0000-0000-000000000001",
                       "email": "iphone", "enabled": True}],
          "hosts": {}}
    rw.update(over)
    return rw


@pytest.mark.skipif(shutil.which("xray") is None, reason="real xray binary not installed")
def test_road_warrior_inbound_passes_real_xray_test():
    """The stub xray always answers "Configuration OK", so nothing else in the suite proves real
    xray accepts what we emit: `flow=xtls-rprx-vision` alongside our field set, `dest` (vs the
    newer `target`), and `sniffing.routeOnly` on a vless inbound. Skips without a binary."""
    node = Node(id=1, name="real", address="1.2.3.4", port=47000,
                uuid="00000000-0000-0000-0000-000000000000", sni="www.microsoft.com",
                public_key=_NODE_PUB, short_id="0123abcd")
    ok, out = validate_config(build_config(node, Settings(), rw_inbound=_rw()), "xray")
    assert ok, out


@pytest.mark.skipif(shutil.which("xray") is None, reason="real xray binary not installed")
def test_road_warrior_lan_hosts_pass_real_xray_test():
    """dns.hosts + the `direct-lan` freedom outbound with domainStrategy=UseIP + the domain rule."""
    node = Node(id=1, name="real", address="1.2.3.4", port=47000,
                uuid="00000000-0000-0000-0000-000000000000", sni="www.microsoft.com",
                public_key=_NODE_PUB, short_id="0123abcd")
    cfg = build_config(node, Settings(), rw_inbound=_rw(hosts={"nas.v2pi": "192.168.1.88"}))
    ok, out = validate_config(cfg, "xray")
    assert ok, out


@pytest.mark.skipif(shutil.which("xray") is None, reason="real xray binary not installed")
def test_empty_short_ids_is_rejected_by_real_xray():
    """Why PUT /api/rw refuses to arm without a short id — the guard is load-bearing, not tidy.

    Answered against Xray 26.3.27 on the gateway: `Failed to build REALITY config. > empty
    "shortIds"`. So without the API guard, arming remote access would emit a config xray will not
    load; the apply would fail validation and roll back, and the operator would be left staring at
    a REALITY build error with no hint that a missing short id caused it.
    """
    node = Node(id=1, name="real", address="1.2.3.4", port=47000,
                uuid="00000000-0000-0000-0000-000000000000", sni="www.microsoft.com",
                public_key=_NODE_PUB, short_id="0123abcd")
    ok, out = validate_config(build_config(node, Settings(), rw_inbound=_rw(short_ids=[])), "xray")
    assert ok is False
    assert "shortIds" in out


@pytest.mark.skipif(shutil.which("xray") is None, reason="real xray binary not installed")
def test_reality_dest_spelling_is_still_accepted():
    """Newer Xray also spells this `target`. Verified on 26.3.27: BOTH are accepted, so the
    `dest` we emit needs no version sniffing. This test is the tripwire if that ever changes."""
    node = Node(id=1, name="real", address="1.2.3.4", port=47000,
                uuid="00000000-0000-0000-0000-000000000000", sni="www.microsoft.com",
                public_key=_NODE_PUB, short_id="0123abcd")
    cfg = build_config(node, Settings(), rw_inbound=_rw())
    reality = next(i for i in cfg["inbounds"]
                   if i["tag"] == "rw-in")["streamSettings"]["realitySettings"]
    assert "dest" in reality
    ok, out = validate_config(cfg, "xray")
    assert ok, out
