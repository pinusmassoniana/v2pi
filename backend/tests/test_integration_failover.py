from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.models import Node
from pi_gw_panel.controller import apply_node
from pi_gw_panel.health import failover
from pi_gw_panel.health.monitor import HealthMonitor


def test_stubbed_probe_failover_switches_active(settings, stub_xray):
    """End-to-end (stubbed probes): the active node's real request fails past the
    hysteresis → the monitor/failover chain switches the active node to a TCP-alive
    candidate, /status reflects it, and cooldown debounces further thrashing."""
    settings.xray_bin = stub_xray
    state = build_state(settings, net=DryRunBackend())
    store = state.store
    a = store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="ua",
                            sni="s", public_key="PK", short_id="sid"))
    b = store.add_node(Node(id=None, name="b", address="2.2.2.2", port=443, uuid="ub",
                            sni="s", public_key="PK", short_id="sid"))
    try:
        # apply A → it becomes the active node
        assert apply_node(store.get_node(a), settings, state.supervisor, state.net,
                          store=store, xray_bin=stub_xray).ok
        assert store.get_setting("active_node_id") == str(a)
        store.set_setting("health_hysteresis", "2")   # smaller threshold for a fast test

        # every node TCP-alive; the active node's real request always fails
        monitor = HealthMonitor(
            state,
            tcp_ping=lambda addr, port: (True, 5),
            real_request=lambda proxy, url: (False, None, None, None),
            now_iso=lambda: "2026-06-03T00:00:00Z",
            after_tick=lambda: failover.run(state, now=1000.0),
        )

        monitor._tick()                                       # fail_count(a)=1 < 2 → hold
        assert store.get_setting("active_node_id") == str(a)
        assert store.get_health(a).fail_count == 1

        monitor._tick()                                       # fail_count(a)=2 ≥ 2 → fail over to B
        assert store.get_setting("active_node_id") == str(b)
        assert store.get_setting("last_failover_at") == "1000.0"

        # /status reflects the switch (no lifespan context → the monitor isn't auto-ticking)
        client = TestClient(create_app(settings, state=state))
        client.post("/api/setup", json={"username": "admin", "password": "changeme"})
        assert client.get("/api/status").json()["active_node_id"] == b

        # B's real request also fails, but cooldown debounces → no thrash back to A
        monitor._tick()
        monitor._tick()
        assert store.get_setting("active_node_id") == str(b)
    finally:
        state.supervisor.stop()
