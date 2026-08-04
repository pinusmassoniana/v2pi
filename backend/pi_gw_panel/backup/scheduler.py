"""Optional daily auto-backup (SN9). Writes a timestamped JSON snapshot to
data_dir/backups/ when `auto_backup_enabled` is on, keeping the most recent `keep`."""
import asyncio
import logging
import os
import time
from pi_gw_panel.backup import backups_dir, export_state, validate_document, write_document

_LAST_BACKUP_KEY = "last_backup_at"   # persisted so a box restarting < interval still backs up
logger = logging.getLogger(__name__)


class BackupScheduler:
    def __init__(self, state, interval_sec: float = 86400.0, keep: int = 7):
        self._state = state
        self._interval = interval_sec
        self._keep = keep
        self._task: asyncio.Task | None = None

    def _enabled(self) -> bool:
        return (self._state.store.get_setting("auto_backup_enabled") or "0") == "1"

    def run_once(self, now: int | None = None) -> str | None:
        if not self._enabled():
            return None
        now = int(time.time()) if now is None else now
        d = backups_dir(self._state.settings)   # backups hold sub creds → dir 0700
        path = os.path.join(d, f"backup-{now}.json")
        doc = export_state(self._state.store)
        doc["created_at"] = now                 # header for a future restore to validate (schema_version already present)
        # A backup that restore would refuse is worse than no backup: it is a daily file that
        # looks like a safety net until the day it is needed. Any drift between what the panel
        # can store and what a document may contain has to surface here, loudly, on the first
        # run after it appears — not silently, months later, during a recovery.
        validate_document(doc)
        write_document(doc, path)
        # Prune to keep-N only AFTER the replace succeeded (a complete file is now on disk).
        kept = sorted(os.path.join(d, x) for x in os.listdir(d)
                      if x.startswith("backup-") and x.endswith(".json"))
        for old in kept[:-self._keep]:
            try:
                os.unlink(old)
            except OSError:
                pass
        self._state.store.set_setting(_LAST_BACKUP_KEY, str(now))   # for the catch-up delay (see _loop)
        return path

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
        """Delay before the first run = interval minus time already elapsed since the last
        backup, floored at 0 (overdue → run immediately). So a box that restarts more often
        than the interval still backs up on schedule instead of never."""
        last = self._state.store.get_setting(_LAST_BACKUP_KEY)
        try:
            last = int(last)
        except (TypeError, ValueError):
            return self._interval               # never backed up → wait a full interval
        return max(0.0, self._interval - (time.time() - last))

    async def _loop(self) -> None:
        delay = self._initial_delay()
        while True:
            await asyncio.sleep(delay)
            try:
                await asyncio.get_running_loop().run_in_executor(None, self.run_once)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("automatic backup failed; will retry on the next interval")
            delay = self._interval
