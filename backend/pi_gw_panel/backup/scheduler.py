"""Optional daily auto-backup (SN9). Writes a timestamped JSON snapshot to
data_dir/backups/ when `auto_backup_enabled` is on, keeping the most recent `keep`."""
import asyncio
import json
import os
import time
from pi_gw_panel.backup import export_state


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
        d = os.path.join(self._state.settings.data_dir, "backups")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"backup-{now if now is not None else int(time.time())}.json")
        with open(path, "w") as f:
            json.dump(export_state(self._state.store), f)
        kept = sorted(os.path.join(d, x) for x in os.listdir(d)
                      if x.startswith("backup-") and x.endswith(".json"))
        for old in kept[:-self._keep]:
            try:
                os.unlink(old)
            except OSError:
                pass
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

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await asyncio.get_running_loop().run_in_executor(None, self.run_once)
