import os
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
    dnsmasq: object | None = None   # DnsmasqSupervisor (segment DHCP + IPv6 RA)
    pd_client: object | None = None # DHCPv6-PD client (odhcp6c) — auto-prefix mode


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
    # The panel owns the host gateway's DHCP + IPv6 RA via its own supervised dnsmasq, and (in
    # `auto` mode) a DHCPv6-PD client. Constructed always; only *applied* by host_provision on
    # the linux backend (dev/CI never spawns them — host_provision early-returns on DryRun).
    from pi_gw_panel.net_control.dnsmasq_supervisor import DnsmasqSupervisor
    from pi_gw_panel.net_control.pd_client import PdClient
    dnsmasq = DnsmasqSupervisor(settings.dnsmasq_bin, os.path.join(settings.data_dir, "dnsmasq.conf"))
    pd_client = PdClient(settings.mgmt_iface, os.path.join(settings.data_dir, "odhcp6c.sh"))

    def _add_data_used(up_delta: int, down_delta: int) -> None:
        """Durably accumulate proxy bytes so "data used" survives an xray restart (audit F)."""
        store.set_setting("data_used_up", str(int(store.get_setting("data_used_up") or "0") + up_delta))
        store.set_setting("data_used_down", str(int(store.get_setting("data_used_down") or "0") + down_delta))

    recorder = TrafficRecorder(
        sampler=TrafficSampler(lambda: stats_client.query("outbound>>>")),
        history=history,
        stats_enabled=lambda: (store.get_setting("stats_enabled") or "1") == "1",
        running=lambda: supervisor.status()["running"],   # don't poke a dead stats port (F5)
        interval_ms=lambda: int(store.get_setting("traffic_sample_ms") or SETTINGS_DEFAULTS["traffic_sample_ms"]),
        on_total=_add_data_used,
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
        dnsmasq=dnsmasq,
        pd_client=pd_client,
    )
