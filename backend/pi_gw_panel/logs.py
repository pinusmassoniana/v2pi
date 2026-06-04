"""Log file tail + app-logging setup (Wave 3a)."""
import logging
import os

_app_logging_configured = False


def tail(path: str, lines: int) -> list[str]:
    """Last `lines` lines of `path`; [] if the file is missing. Reads from the END in blocks
    (O(lines), not O(filesize)) so a large xray access log doesn't get slurped whole."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            data = b""
            block = 8192
            while pos > 0 and data.count(b"\n") <= lines:
                read = min(block, pos)
                pos -= read
                f.seek(pos)
                data = f.read(read) + data
        return data.decode("utf-8", "replace").splitlines()[-lines:]
    except OSError:
        return []


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
