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


class _OwnerOnlyRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler whose every generation is created 0600.

    The base class opens each new file through `open()`, so its permissions come from the
    process umask — a rollover would otherwise hand back a world-readable log."""

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(fd, "a", encoding=self.encoding)


def setup_app_logging(path: str) -> logging.Handler:
    """Attach and return the exact handler owned by one application lifespan."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    handler = _OwnerOnlyRotatingFileHandler(path, maxBytes=5_000_000, backupCount=3)
    # A log predating this (or a directory created by something else) keeps its old mode
    # until it is fixed here; failure to tighten it must not stop the panel from booting.
    for target, mode in ((directory, 0o700), (path, 0o600)):
        try:
            os.chmod(target, mode)
        except OSError:
            logging.getLogger(__name__).warning("could not secure log path %s", target)
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
