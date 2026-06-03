import json
import os
import shutil
import subprocess
import tempfile
from pi_gw_panel.config import Settings


def validate_config(cfg: dict, xray_bin: str) -> tuple[bool, str]:
    """Run `xray -test -config <tmp>`; return (ok, combined-output)."""
    fd, tmp = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f)
        proc = subprocess.run(
            [xray_bin, "-test", "-config", tmp],
            capture_output=True, text=True,
        )
        return proc.returncode == 0, (proc.stdout + proc.stderr).strip()
    finally:
        os.unlink(tmp)


class ConfigManager:
    """Owns the on-disk live config + last-good snapshot + rollback."""

    def __init__(self, settings: Settings, xray_bin: str | None = None):
        self.settings = settings
        self.xray_bin = xray_bin or settings.xray_bin

    def apply(self, cfg: dict) -> tuple[bool, str]:
        ok, out = validate_config(cfg, self.xray_bin)
        if not ok:
            return False, out
        # Snapshot the currently-live config as the rollback (undo) target, THEN write the
        # new one — so rollback() reverts to the *previous* apply, not the one just made.
        if os.path.exists(self.settings.config_path):
            shutil.copyfile(self.settings.config_path, self.settings.lastgood_path)
        self._write_atomic(self.settings.config_path, cfg)
        return True, out

    def rollback(self) -> bool:
        path = self.settings.lastgood_path
        if not os.path.exists(path):
            return False
        try:
            with open(path) as f:
                json.load(f)  # refuse to promote a corrupt/partial snapshot
        except (OSError, json.JSONDecodeError):
            return False
        shutil.copyfile(path, self.settings.config_path)
        return True

    @staticmethod
    def _write_atomic(path: str, cfg: dict) -> None:
        """Write cfg as JSON via temp file + os.replace (atomic on POSIX)."""
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".json", dir=parent or None)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(cfg, f, indent=2)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
