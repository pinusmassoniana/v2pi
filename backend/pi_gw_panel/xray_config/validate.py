import json
import os
import subprocess
import tempfile
from pi_gw_panel.config import Settings

# xray -test can echo config content on error; these keys carry credentials/secrets that must
# never reach logs or an API response (the VLESS uuid is effectively the exit-node password).
_SECRET_KEYS = {
    "uuid", "id", "password", "publickey", "privatekey", "shortid",
    "public_key", "private_key", "short_id",
}
_VALIDATE_TIMEOUT = 15.0


def _collect_secrets(obj, out: set[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and v and k.lower() in _SECRET_KEYS:
                out.add(v)
            else:
                _collect_secrets(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_secrets(v, out)


def scrub_output(text: str, cfg: dict) -> str:
    secrets: set[str] = set()
    _collect_secrets(cfg, secrets)
    for s in secrets:
        text = text.replace(s, "***")
    return text


def validate_config(cfg: dict, xray_bin: str) -> tuple[bool, str]:
    """Run `xray -test -config <tmp>`; return (ok, combined-output with secrets scrubbed)."""
    fd, tmp = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f)
        try:
            proc = subprocess.run(
                [xray_bin, "-test", "-config", tmp],
                capture_output=True, text=True, timeout=_VALIDATE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            # a hung -test would otherwise wedge the whole apply path (it runs synchronously
            # before any write/restart); the child is already killed by run() on timeout.
            return False, "validation timed out"
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                return False, "xray executable not found"
            return False, f"unable to run xray validation: {exc.strerror or type(exc).__name__}"
        return proc.returncode == 0, scrub_output((proc.stdout + proc.stderr).strip(), cfg)
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
            with open(self.settings.config_path) as f:
                previous = json.load(f)
            self._write_atomic(self.settings.lastgood_path, previous)
        self._write_atomic(self.settings.config_path, cfg)
        return True, out

    def rollback(self) -> bool:
        path = self.settings.lastgood_path
        if not os.path.exists(path):
            return False
        try:
            with open(path) as f:
                lastgood = json.load(f)  # refuse to promote a corrupt/partial snapshot
        except (OSError, json.JSONDecodeError):
            return False
        self._write_atomic(self.settings.config_path, lastgood)
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
