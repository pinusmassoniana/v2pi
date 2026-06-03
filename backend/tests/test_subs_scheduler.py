import asyncio
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.models import Subscription
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.subs import service
from pi_gw_panel.subs.scheduler import SubScheduler


class _State:
    def __init__(self, store):
        self.store = store


def _state(settings):
    # match production wiring (build_state) so the scheduler's worker thread can use the conn
    conn = connect(settings.db_path, check_same_thread=False)
    init_schema(conn)
    return _State(NodeStore(conn))


def test_due_subs_respects_interval(settings):
    st = _state(settings)
    sid = st.store.add_subscription(Subscription(id=None, name="s", url="u", interval_sec=100))
    st.store.add_subscription(Subscription(id=None, name="manual", url="u", interval_sec=0))
    sched = SubScheduler(st)
    assert [s.id for s in sched.due_subs(now=1000.0)] == [sid]   # last_run None → due
    sched._last_run[sid] = 1000.0
    assert sched.due_subs(now=1050.0) == []                      # 50 < 100
    assert [s.id for s in sched.due_subs(now=1100.0)] == [sid]   # 100 elapsed


def test_run_once_refreshes_due(monkeypatch, settings):
    st = _state(settings)
    sid = st.store.add_subscription(Subscription(id=None, name="s", url="u", interval_sec=10))
    calls = []
    monkeypatch.setattr(service, "refresh", lambda state, sub: calls.append(sub.id))
    sched = SubScheduler(st)
    sched.run_once(now=500.0)
    assert calls == [sid] and sched._last_run[sid] == 500.0


def test_loop_starts_and_cancels(monkeypatch, settings):
    st = _state(settings)
    st.store.add_subscription(Subscription(id=None, name="s", url="u", interval_sec=1))
    calls = []
    monkeypatch.setattr(service, "refresh", lambda state, sub: calls.append(sub.id))

    async def drive():
        sched = SubScheduler(st, tick_sec=0.001)
        sched.start()
        await asyncio.sleep(0.05)
        await sched.stop()

    asyncio.run(drive())
    assert calls  # refreshed at least once before cancellation
