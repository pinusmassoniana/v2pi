import os
import pytest
from pi_gw_panel.config import Settings
from pi_gw_panel.__main__ import ensure_bootstrap_token, ensure_session_secret


def test_from_env_defaults():
    s = Settings.from_env({})
    assert s.bind_host == "0.0.0.0"                        # reachable by default (auth-gated)
    assert s.xray_bin == "xray"
    assert s.data_dir == "data"
    assert s.db_path == os.path.join("data", "pi_gw_panel.sqlite")
    assert s.config_path == os.path.join("data", "xray.json")
    assert s.lastgood_path == os.path.join("data", "xray.lastgood.json")
    assert s.session_secret == ""                          # resolved at boot, not in from_env
    assert s.static_dir.endswith("pi_gw_panel/static") or s.static_dir == ""


def test_from_env_overrides_and_data_nesting():
    s = Settings.from_env({
        "PI_GW_BIND_HOST": "192.168.1.120", "PI_GW_DATA_DIR": "/data",
        "PI_GW_XRAY_BIN": "/usr/local/bin/xray", "PI_GW_STATIC_DIR": "/srv/spa",
        "PI_GW_SESSION_SECRET": "s" * 32,
        "PI_GW_MGMT_IFACE": "enp1s0", "PI_GW_MGMT_IP": "10.0.0.2",
        "PI_GW_SEGMENT_IFACE": "enp2s0.20", "PI_GW_SEGMENT_IP": "10.20.0.1",
        "PI_GW_DHCP_START": "10.20.0.30", "PI_GW_DHCP_END": "10.20.0.200",
        "PI_GW_DHCP_LEASE": "6h", "PI_GW_CLIENT_DNS": "9.9.9.9",
        "PI_GW_TLS_CERT": "/data/tls.crt", "PI_GW_TLS_KEY": "/data/tls.key",
    })
    assert s.bind_host == "192.168.1.120"
    assert s.data_dir == "/data"
    assert s.db_path == "/data/pi_gw_panel.sqlite"
    assert s.config_path == "/data/xray.json"
    assert s.lastgood_path == "/data/xray.lastgood.json"
    assert s.xray_bin == "/usr/local/bin/xray"
    assert s.static_dir == "/srv/spa"
    assert s.session_secret == "s" * 32
    assert (s.mgmt_iface, s.mgmt_ip) == ("enp1s0", "10.0.0.2")
    assert (s.segment_iface, s.segment_ip) == ("enp2s0.20", "10.20.0.1")
    assert (s.dhcp_start, s.dhcp_end, s.dhcp_lease) == ("10.20.0.30", "10.20.0.200", "6h")
    assert s.client_dns == "9.9.9.9"
    assert (s.tls_cert, s.tls_key) == ("/data/tls.crt", "/data/tls.key")


def test_short_explicit_session_secret_is_rejected():
    with pytest.raises(ValueError, match="at least 32 bytes"):
        Settings.from_env({"PI_GW_SESSION_SECRET": "too-short"})


def test_tls_pair_must_be_complete():
    with pytest.raises(ValueError, match="together"):
        Settings.from_env({"PI_GW_TLS_CERT": "/tmp/cert"})


def test_ensure_session_secret_generates_persists_and_is_stable(tmp_path):
    data = str(tmp_path / "data")
    s1 = ensure_session_secret(data)
    assert s1 and len(s1) >= 32                            # a strong generated secret
    assert os.path.isfile(os.path.join(data, "session_secret"))
    s2 = ensure_session_secret(data)                       # second call reads the persisted one
    assert s2 == s1                                         # stable across calls / restarts


def test_ensure_session_secret_reads_existing(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "session_secret").write_text("p" * 40)
    assert ensure_session_secret(str(data)) == "p" * 40


def test_bootstrap_token_is_persisted_0600_and_stable(tmp_path):
    data = str(tmp_path / "data")
    first = ensure_bootstrap_token(data)
    second = ensure_bootstrap_token(data)
    path = tmp_path / "data" / "bootstrap_token"
    assert first == second and len(first) >= 32
    assert path.stat().st_mode & 0o777 == 0o600
