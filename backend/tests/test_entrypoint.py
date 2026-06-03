import os
from pi_gw_panel.config import Settings
from pi_gw_panel.__main__ import ensure_session_secret


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
        "PI_GW_SESSION_SECRET": "real-secret",
    })
    assert s.bind_host == "192.168.1.120"
    assert s.data_dir == "/data"
    assert s.db_path == "/data/pi_gw_panel.sqlite"
    assert s.config_path == "/data/xray.json"
    assert s.lastgood_path == "/data/xray.lastgood.json"
    assert s.xray_bin == "/usr/local/bin/xray"
    assert s.static_dir == "/srv/spa"
    assert s.session_secret == "real-secret"


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
    (data / "session_secret").write_text("preset-secret-value")
    assert ensure_session_secret(str(data)) == "preset-secret-value"
