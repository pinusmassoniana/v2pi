import os
import stat
import textwrap
import pytest
from pi_gw_panel.config import Settings


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
