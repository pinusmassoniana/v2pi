import os
import stat
import textwrap
import pytest
from fastapi.testclient import TestClient
from pi_gw_panel.config import Settings
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.net_control.dryrun import DryRunBackend

# Synthetic, non-secret password used to complete first-run setup in tests. Any value
# satisfying the API's min_length=8 works; the exact value is asserted nowhere.
_TEST_PASSWORD = "changeme"


@pytest.fixture
def settings(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    return Settings(
        data_dir=str(data),
        db_path=str(data / "test.sqlite"),
        config_path=str(data / "xray.json"),
        lastgood_path=str(data / "xray.lastgood.json"),
    )


@pytest.fixture
def stub_xray(tmp_path):
    """A fake xray: `-test` exits per STUB_XRAY_FAIL env; run mode sleeps."""
    path = tmp_path / "xray"
    path.write_text(textwrap.dedent("""\
        #!/bin/sh
        for a in "$@"; do
          if [ "$a" = "-test" ]; then
            if [ "$STUB_XRAY_FAIL" = "1" ]; then
              echo "config error: stub forced failure" >&2
              exit 1
            fi
            echo "Configuration OK"
            exit 0
          fi
        done
        exec sleep 300
    """))
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def _build_dryrun_state(settings, stub_xray):
    """Real build_state wiring with the dry-run net backend + stub xray binary — the state
    half of the app-bootstrap shared by most API test modules."""
    settings.xray_bin = stub_xray
    return build_state(settings, net=DryRunBackend())


def _client(settings, stub_xray):
    """Bootstrap a TestClient against the real app + dry-run state. Import this instead of
    pasting another copy — it used to be duplicated (with drift) across ~20 test modules."""
    return TestClient(create_app(settings, state=_build_dryrun_state(settings, stub_xray)))


def _login(c, password=_TEST_PASSWORD):
    """Complete first-run setup on TestClient `c` and return the CSRF token needed for
    subsequent authenticated (mutating) requests. Import this instead of pasting another
    copy — it used to be duplicated (with drift in password and return shape) across ~16
    test modules. Callers needing a ready-to-use headers dict should build
    `{"X-CSRF-Token": _login(c)}` themselves."""
    c.post("/api/setup", json={"username": "admin", "password": password})
    return c.get("/api/csrf").json()["csrf"]
