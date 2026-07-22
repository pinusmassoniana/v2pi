"""Bounded log tail and explicitly owned application log handlers."""
import logging
import logging.handlers
import os


def tail(path: str, lines: int) -> list[str]:
    """Last `lines` lines of `path`; [] if the file is missing. Reads from the END in blocks
    (O(lines), not O(filesize)) so a large xray access log doesn't get slurped whole."""
    if lines <= 0:                      # 0/negative → no lines wanted (splitlines()[-0:] would return the whole file)
        return []
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


def setup_app_logging(path: str) -> logging.Handler:
    """Attach and return the exact handler owned by one application lifespan."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=5_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return handler


def teardown_app_logging(handler: logging.Handler | None) -> None:
    """Detach and close one lifespan-owned handler; safe after partial startup."""
    if handler is None:
        return
    root = logging.getLogger()
    if handler in root.handlers:
        root.removeHandler(handler)
    handler.close()
