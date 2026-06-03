import os

from pi_gw_panel import __version__
from pi_gw_panel.config import Settings
from pi_gw_panel.models import Node


def test_version_is_integer_chain():
    # Versioning policy: pre-1.0 integer chain; MAJOR stays 0 until first milestone,
    # and every segment is an uncapped integer. Robust across per-task version bumps.
    parts = __version__.split(".")
    assert parts[0] == "0"
    assert all(p.isdigit() for p in parts)


def test_settings_defaults():
    s = Settings()
    assert s.tproxy_port == 52345
    assert s.table == 100
    assert s.xray_bin == "xray"
    assert s.doh_url.startswith("https://")


def test_node_minimal_fields():
    n = Node(id=None, name="n1", address="1.2.3.4", port=47000, uuid="u-1")
    assert n.transport == "vision"
    assert n.flow == "xtls-rprx-vision"


def test_settings_base_dir_resolves_relative_paths(tmp_path):
    s = Settings(base_dir=str(tmp_path))
    assert s.db_path == os.path.join(str(tmp_path), "data/pi_gw_panel.sqlite")
    # absolute paths are left untouched
    s2 = Settings(base_dir=str(tmp_path), config_path="/abs/xray.json")
    assert s2.config_path == "/abs/xray.json"


def test_settings_ensure_dirs_creates_data_dir(tmp_path):
    d = tmp_path / "x" / "data"
    s = Settings(data_dir=str(d), db_path=str(d / "db"),
                 config_path=str(d / "c"), lastgood_path=str(d / "l"))
    s.ensure_dirs()
    assert os.path.isdir(str(d))


def test_node_xhttp_clears_flow():
    n = Node(id=None, name="x", address="1.2.3.4", port=8443, uuid="u", transport="xhttp")
    assert n.flow == ""  # flow is Vision-only
