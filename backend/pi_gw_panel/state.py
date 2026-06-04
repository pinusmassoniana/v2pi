from dataclasses import dataclass
from pi_gw_panel.config import Settings, SETTINGS_DEFAULTS
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.xray_supervisor.supervisor import XraySupervisor
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.stats.client import StatsClient
from pi_gw_panel.stats.sampler import TrafficSampler
from pi_gw_panel.stats.history import TrafficHistory, TrafficRecorder


@dataclass
class AppState:
    settings: Settings
    store: NodeStore
    supervisor: XraySupervisor
    net: object              # NetBackend (duck-typed: apply_tproxy/teardown)
    xray_bin: str | None = None
    sampler: object | None = None   # TrafficSampler for the live WS (stubbed in WS tests)
    history: object | None = None   # TrafficHistory ring buffer (long-window graph)
    recorder: object | None = None  # TrafficRecorder background task (started in lifespan)


def build_state(settings: Settings, net: object | None = None) -> AppState:
    """Wire the real backbone from settings (production startup).

    `net` lets tests inject a DryRunBackend while still exercising THIS real
    wiring path; production passes None and gets select_backend(settings).
    """
    settings.ensure_dirs()
    conn = connect(settings.db_path, check_same_thread=False)
    init_schema(conn)
    store = NodeStore(conn)
    if net is None:
        from pi_gw_panel.net_control.factory import select_backend
        net = select_backend(settings)
    # Stats client → sampler for the live traffic graph. The channel is built lazily
    # on first query, so constructing this touches no network (safe in tests).
    port = int(store.get_setting("stats_api_port") or SETTINGS_DEFAULTS["stats_api_port"])
    stats_client = StatsClient(f"127.0.0.1:{port}")
    sampler = TrafficSampler(lambda: stats_client.query("outbound>>>"))
    # Always-on history: a SECOND sampler (independent prev-counters) feeds a 1h ring
    # buffer (3600 @ 1s) so the graph has a full window the moment the Dashboard opens.
    history = TrafficHistory(maxlen=3600)
    supervisor = XraySupervisor(settings.xray_bin, settings.config_path)
    recorder = TrafficRecorder(
        sampler=TrafficSampler(lambda: stats_client.query("outbound>>>")),
        history=history,
        stats_enabled=lambda: (store.get_setting("stats_enabled") or "1") == "1",
        running=lambda: supervisor.status()["running"],   # don't poke a dead stats port (F5)
        interval_ms=lambda: int(store.get_setting("traffic_sample_ms") or SETTINGS_DEFAULTS["traffic_sample_ms"]),
    )
    return AppState(
        settings=settings,
        store=store,
        supervisor=supervisor,
        net=net,
        xray_bin=settings.xray_bin,
        sampler=sampler,
        history=history,
        recorder=recorder,
    )
