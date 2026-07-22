import asyncio
import os

from pi_gw_panel.backup.scheduler import BackupScheduler
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore


class _State:
    def __init__(self, settings):
        self.settings = settings
        connection = connect(settings.db_path)
        init_schema(connection)
        self.store = NodeStore(connection)
        self.store.set_setting("auto_backup_enabled", "1")


def test_run_once_fsyncs_directory_and_leaves_no_temp(settings, monkeypatch):
    state = _State(settings)
    scheduler = BackupScheduler(state, keep=2)
    real_fsync = os.fsync
    fsynced = []

    def observing_fsync(fd):
        fsynced.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", observing_fsync)
    path = scheduler.run_once(now=123)
    assert os.path.isfile(path)
    assert len(fsynced) >= 2  # file contents + containing directory metadata
    assert not any(name.endswith(".tmp") for name in os.listdir(os.path.dirname(path)))


def test_loop_survives_one_iteration_failure(settings):
    state = _State(settings)
    scheduler = BackupScheduler(state, interval_sec=0.01)
    calls = {"count": 0}

    async def scenario():
        loop = asyncio.get_running_loop()

        def flaky():
            calls["count"] += 1
            if calls["count"] == 1:
                raise OSError("disk full")
            loop.call_soon_threadsafe(scheduler._task.cancel)

        scheduler.run_once = flaky
        scheduler._initial_delay = lambda: 0.0
        scheduler.start()
        try:
            await scheduler._task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert calls["count"] == 2
