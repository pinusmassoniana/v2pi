import os
import logging
from dataclasses import dataclass
from pi_gw_panel.config import Settings, SETTINGS_DEFAULTS, safe_int
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.xray_supervisor.supervisor import XraySupervisor
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.stats.client import StatsClient
from pi_gw_panel.stats.sampler import TrafficSampler
from pi_gw_panel.stats.history import TrafficHistory, TrafficRecorder


logger = logging.getLogger(__name__)


@dataclass
class AppState:
    settings: Settings
    store: NodeStore
    supervisor: XraySupervisor
    net: object              # NetBackend (duck-typed: apply_tproxy/teardown)
    xray_bin: str | None = None
    stats_client: object | None = None
    sampler: object | None = None   # sole TrafficSampler, owned/driven by recorder
    history: object | None = None   # TrafficHistory ring buffer (long-window graph)
    recorder: object | None = None  # TrafficRecorder background task (started in lifespan)
    dnsmasq: object | None = None   # DnsmasqSupervisor (segment DHCP + IPv6 RA)
    pd_client: object | None = None # DHCPv6-PD client (odhcp6c) — auto-prefix mode
    provision_result: object | None = None  # last host_provision NetResult; /api/ready fails closed

    def close(self) -> None:
        """Release non-task resources after all background owners have stopped."""
        for name, resource in (
                ("supervisor", self.supervisor),
                ("stats client", self.stats_client),
                ("store", self.store)):
            close = getattr(resource, "close", None)
            if close is None and name == "supervisor":
                close = getattr(resource, "stop", None)
            if close is None:
                continue
            try:
                close()
            except Exception:
                logger.warning("failed to close %s", name, exc_info=True)


def build_state(settings: Settings, net: object | None = None) -> AppState:
    """Wire the real backbone from settings (production startup).

    `net` lets tests inject a DryRunBackend while still exercising THIS real
    wiring path; production passes None and gets select_backend(settings).
    """
    settings.ensure_dirs()
    from pi_gw_panel.health import geo
    geo.configure(settings.geoip_db)        # egress IP→country flag (no-op if the db is absent)
    conn = connect(settings.db_path, check_same_thread=False)
    init_schema(conn)
    store = NodeStore(conn)
    if net is None:
        from pi_gw_panel.net_control.factory import select_backend
        net = select_backend(settings)
    # Stats client → sampler for the live traffic graph. The channel is built lazily
    # on first query, so constructing this touches no network (safe in tests).
    # safe-int so a corrupt cosmetic setting (e.g. from a bad restore) can't block boot (audit P2)
    port = safe_int(store.get_setting("stats_api_port"),
                    int(SETTINGS_DEFAULTS["stats_api_port"]), "stats_api_port")
    stats_client = StatsClient(f"127.0.0.1:{port}")
    sampler = TrafficSampler(lambda: stats_client.query("outbound>>>"))
    # Always-on history + immutable live frame are produced by this one sampler. WebSockets
    # only consume recorder snapshots, so clients cannot multiply gRPC calls.
    history = TrafficHistory(maxlen=3600)

    def _xray_listening() -> bool:
        """B1 readiness probe: the tproxy dokodemo inbound answers a local TCP connect the
        moment xray is up (it is always present in a node config)."""
        import socket
        try:
            socket.create_connection(("127.0.0.1", settings.tproxy_port), timeout=0.1).close()
            return True
        except OSError:
            return False

    # Only the real Pi backend gets the readiness gate — dev/CI run a stub xray that never
    # listens, and waiting the full budget there would slow every test apply.
    ready = _xray_listening if type(net).__name__ == "LinuxBackend" else None
    supervisor = XraySupervisor(settings.xray_bin, settings.config_path, ready_check=ready)
    # The panel owns the host gateway's DHCP + IPv6 RA via its own supervised dnsmasq, and (in
    # `auto` mode) a DHCPv6-PD client. Constructed always; only *applied* by host_provision on
    # the linux backend (dev/CI never spawns them — host_provision early-returns on DryRun).
    from pi_gw_panel.net_control.dnsmasq_supervisor import DnsmasqSupervisor
    from pi_gw_panel.net_control.pd_client import PdClient
    dnsmasq = DnsmasqSupervisor(settings.dnsmasq_bin, os.path.join(settings.data_dir, "dnsmasq.conf"))
    pd_client = PdClient(settings.mgmt_iface, os.path.join(settings.data_dir, "odhcp6c.sh"))

    def _add_data_used(up_delta: int, down_delta: int) -> None:
        """Durably accumulate proxy bytes so "data used" survives an xray restart (audit F)."""
        with store.transaction():
            store.set_setting(
                "data_used_up", str(int(store.get_setting("data_used_up") or "0") + up_delta))
            store.set_setting(
                "data_used_down", str(int(store.get_setting("data_used_down") or "0") + down_delta))

    recorder = TrafficRecorder(
        sampler=sampler,
        history=history,
        stats_enabled=lambda: (store.get_setting("stats_enabled") or "1") == "1",
        running=lambda: supervisor.status()["running"],   # don't poke a dead stats port (F5)
        interval_ms=lambda: store.get_setting("traffic_sample_ms")
        or SETTINGS_DEFAULTS["traffic_sample_ms"],
        on_total=_add_data_used,
        on_minute=store.add_traffic_minute,   # N4: durable 1-min downsample for 24h/7d windows
    )
    return AppState(
        settings=settings,
        store=store,
        supervisor=supervisor,
        net=net,
        xray_bin=settings.xray_bin,
        stats_client=stats_client,
        sampler=sampler,
        history=history,
        recorder=recorder,
        dnsmasq=dnsmasq,
        pd_client=pd_client,
    )
