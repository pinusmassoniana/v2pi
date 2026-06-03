"""Log file tail + app-logging setup (Wave 3a)."""
import logging
import os

_app_logging_configured = False


def tail(path: str, lines: int) -> list[str]:
    """Last `lines` lines of `path`; [] if the file is missing. Reads the whole file
    (panel logs are small / capped by the caller)."""
    if not os.path.exists(path):
        return []
    with open(path, "r", errors="replace") as f:
        return f.read().splitlines()[-lines:]


def setup_app_logging(path: str) -> None:
    """Attach a single root file handler so app logs land in `path`. Idempotent —
    only the first call wires a handler (production runs it once at startup)."""
    global _app_logging_configured
    if _app_logging_configured:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    handler = logging.FileHandler(path)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    _app_logging_configured = True
