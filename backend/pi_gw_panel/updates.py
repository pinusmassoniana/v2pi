"""B2: is a newer panel or xray published? Checked daily, reported, never installed.

Deploying is a digest re-pin on the gateway plus `compose up -d`, and the container has no
Docker socket on purpose — so this only ever ANSWERS the question "am I behind?", which is the
one the operator cannot answer from inside the panel today.

Both checks are one GET each against the GitHub releases API, through the tunnel whenever one is
up (the same choice subscription fetches make). Failure is not an error state: a box with no
internet is a normal box, and the card says when the last check was and why it failed.
"""
import asyncio
import json
import logging
import re
import time

from pi_gw_panel.subs.fetcher import fetch_url
from pi_gw_panel.subs.service import tunnel_proxy

log = logging.getLogger(__name__)

PANEL_REPO = "pinusmassoniana/v2pi"
XRAY_REPO = "XTLS/Xray-core"
_LATEST_URL = "https://api.github.com/repos/{repo}/releases/latest"
_TIMEOUT = 10.0

# Settings keys. Only `update_check_enabled` is configuration (and backed up); the rest is the
# last answer, which a restore must not carry onto another box.
ENABLED_KEY = "update_check_enabled"
PANEL_KEY = "update_latest_panel"
XRAY_KEY = "update_latest_xray"
CHECKED_KEY = "update_checked_at"
ERROR_KEY = "update_check_error"

_VERSION_RE = re.compile(r"(\d+(?:\.\d+)+)")


def parse_version(text) -> tuple[int, ...] | None:
    """First dotted integer run in `text` → a tuple. Handles a release tag ("v2.1"), the panel's
    own version ("2.0.1.3.4") and xray's `-version` first line ("Xray 26.3.27 (…)")."""
    match = _VERSION_RE.search(str(text or ""))
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(latest, current) -> bool:
    """True when `latest` is a strictly higher version than `current`.

    Segments are uncapped integers and a shorter chain is the EARLIER release of that tier
    (0.6 precedes 0.6.1), so the shorter side is zero-padded rather than treated as a prefix.
    Anything unparseable answers False: an unreadable version is not a reason to nag.
    """
    left, right = parse_version(latest), parse_version(current)
    if left is None or right is None:
        return False
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)) > right + (0,) * (width - len(right))


def _latest_tag(repo: str, proxy: str | None) -> str:
    body, _headers = fetch_url(_LATEST_URL.format(repo=repo),
                               headers={"accept": "application/vnd.github+json",
                                        "user-agent": "v2pi-update-check"},
                               proxy=proxy, timeout=_TIMEOUT)
    tag = json.loads(body).get("tag_name")
    if not isinstance(tag, str) or not tag.strip():
        raise ValueError(f"{repo}: release has no tag_name")
    return tag.strip()[:64]


def check_now(state, *, force: bool = False) -> dict:
    """One check of both repos. `force` runs it even when the daily check is switched off, so
    the button on the panel works without making the operator turn the schedule on first."""
    store = state.store
    if not force and (store.get_setting(ENABLED_KEY) or "1") != "1":
        return {"checked": False}
    proxy = tunnel_proxy(state)
    errors = []
    for repo, key in ((PANEL_REPO, PANEL_KEY), (XRAY_REPO, XRAY_KEY)):
        try:
            store.set_setting(key, _latest_tag(repo, proxy))
        except Exception as exc:                      # offline, rate-limited, 404 — all the same
            errors.append(f"{repo.split('/')[-1]}: {type(exc).__name__}")
            log.info("update check failed for %s: %s", repo, exc)
    store.set_setting(CHECKED_KEY, str(int(time.time())))
    store.set_setting(ERROR_KEY, "; ".join(errors)[:200])
    return {"checked": True, "panel": store.get_setting(PANEL_KEY) or "",
            "xray": store.get_setting(XRAY_KEY) or "", "error": "; ".join(errors)[:200]}


class UpdateScheduler:
    """Daily check, with the same catch-up delay as the backup scheduler: a box that restarts
    more often than the interval still checks on schedule instead of never."""

    def __init__(self, state, interval_sec: float = 86400.0):
        self._state = state
        self._interval = interval_sec
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _initial_delay(self) -> float:
        try:
            last = int(self._state.store.get_setting(CHECKED_KEY))
        except (TypeError, ValueError):
            return 60.0                     # never checked → shortly after boot, not a day later
        return max(0.0, self._interval - (time.time() - last))

    async def _loop(self) -> None:
        delay = self._initial_delay()
        while True:
            await asyncio.sleep(delay)
            try:
                await asyncio.get_running_loop().run_in_executor(None, check_now, self._state)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("update check failed; will retry on the next interval")
            delay = self._interval
