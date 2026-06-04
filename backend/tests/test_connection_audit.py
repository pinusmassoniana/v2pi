"""Backend coverage for the 2026-06-04 connection & network-environment audit (v1.7→v1.8).

A1 — kill-switch is fail-closed: stop/disconnect/boot keep the leak-guard when it's on.
A2 — IPv6 leak-guard (ip6 drop) renders/loads when the kill-switch is on.
B1 — xray watchdog restarts a crashed xray.
B2 — fast active-node real-probe advances fail_count and drives failover.
C1 — uplink reachability probe + injection.
N1/N2 — wan_blocked flag + connection-event log surfaced on /network.
sync_net/boot_guard — apply the right plan for the current tunnel state.
"""
from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.config import Settings
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.models import Node, NodeHealth
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.net_control.linux import LinuxBackend
from pi_gw_panel.net_control.plan import NetPlan
from pi_gw_panel.net_control.render import render_nft6
from pi_gw_panel.net_control import netcheck, events as conn_events
from pi_gw_panel.controller import stop_net, sync_net, boot_guard
from pi_gw_panel.health.liveness import LivenessLoop


def _store():
    conn = connect(":memory:")
    init_schema(conn)
    return NodeStore(conn)


class _FakeSup:
    def __init__(self, state="working"):
        self._state = state
        self.started = 0

    def state(self):
        return self._state

    def start(self):
        self.started += 1
        self._state = "working"

    def status(self):
        return {"running": self._state == "working", "pid": 1 if self._state == "working" else None}


class _State:
    def __init__(self, store, settings, sup=None, net=None):
        self.store, self.settings = store, settings
        self.supervisor = sup or _FakeSup()
        self.net = net or DryRunBackend()
        self.xray_bin = settings.xray_bin


class FakeRun:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, input=None):
        self.calls.append((cmd, input))
        import subprocess
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def cmds(self):
        return [c for c, _ in self.calls]


# --- A1: fail-closed stop / sync / boot ------------------------------------

def test_stop_net_keeps_leakguard_when_killswitch_on():
    store, net = _store(), DryRunBackend()
    store.set_setting("kill_switch_enabled", "1")
    stop_net(Settings(), net, store)
    guard = net.applied[-1]
    assert "chain forward" in guard and " drop" in guard   # leak-guard installed
    assert "tproxy ip to" not in guard                      # no tproxy (tunnel down)


def test_stop_net_full_teardown_when_killswitch_off():
    store, net = _store(), DryRunBackend()
    net.apply_tproxy(NetPlan.from_settings(Settings()))     # seed an applied rule
    stop_net(Settings(), net, store)
    assert net.applied == []                                # teardown cleared everything


def test_sync_net_picks_tproxy_up_guard_down():
    settings = Settings()
    store, net = _store(), DryRunBackend()
    store.set_setting("kill_switch_enabled", "1")
    # down (no active node): guard
    sync_net(_State(store, settings, _FakeSup("stopped"), net))
    assert "tproxy ip to" not in net.applied[-1] and " drop" in net.applied[-1]
    # up (running + active node): full tproxy
    nid = store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u"))
    store.set_setting("active_node_id", str(nid))
    sync_net(_State(store, settings, _FakeSup("working"), net))
    assert "tproxy ip to" in net.applied[-1]


def test_boot_guard_only_when_killswitch_on():
    settings = Settings()
    store, net = _store(), DryRunBackend()
    boot_guard(_State(store, settings, _FakeSup("stopped"), net))
    assert net.applied == []                                # off → no guard
    store.set_setting("kill_switch_enabled", "1")
    boot_guard(_State(store, settings, _FakeSup("stopped"), net))
    assert " drop" in net.applied[-1]                       # on → leak-guard pre-installed


# --- A2: IPv6 leak-guard ----------------------------------------------------

def test_render_nft6_present_only_with_killswitch():
    p = NetPlan.from_settings(Settings())
    assert render_nft6(p) == ""                             # off → no v6 table
    p.kill_switch = True
    t = render_nft6(p)
    assert "table ip6 pi_gw_panel" in t
    # the leak-guard drop now also bypasses multicast (ff00::/8) — unified with the tproxy local set
    assert 'iifname "eth0.2" ip6 daddr != { ::1/128, fe80::/10, fc00::/7, ff00::/8 } drop' in t


def test_linux_apply_guard_drops_no_tproxy_and_loads_v6():
    fake = FakeRun()
    plan = NetPlan.from_settings(Settings())
    plan.kill_switch = True
    res = LinuxBackend(Settings(), run=fake).apply_guard(plan)
    assert res.ok is True
    nft = next(i for c, i in fake.calls if c[:2] == ["nft", "-f"])
    assert "chain forward" in nft and "tproxy ip to" not in nft
    assert "table ip6 pi_gw_panel" in nft                   # v6 leak-guard loaded
    assert ["ip", "rule", "add", "fwmark", "0x40", "lookup", "100"] not in fake.cmds()


def test_linux_teardown_removes_both_families():
    fake = FakeRun()
    LinuxBackend(Settings(), run=fake).teardown()
    assert ["nft", "delete", "table", "ip", "pi_gw_panel"] in fake.cmds()
    assert ["nft", "delete", "table", "ip6", "pi_gw_panel"] in fake.cmds()


# --- B1: xray watchdog ------------------------------------------------------

def test_watchdog_restarts_crashed_xray():
    store, settings = _store(), Settings()
    state = _State(store, settings, _FakeSup("error"))       # wanted-up but died
    LivenessLoop(state)._watchdog()
    assert state.supervisor.started == 1
    assert any(e["kind"] == "xray-restart" for e in conn_events.recent(store))


def test_watchdog_noop_on_deliberate_stop():
    store, settings = _store(), Settings()
    state = _State(store, settings, _FakeSup("stopped"))     # intentional stop
    LivenessLoop(state)._watchdog()
    assert state.supervisor.started == 0


# --- B2: fast active probe + failover event ---------------------------------

def test_liveness_probe_advances_fail_count_on_failure():
    store, settings = _store(), Settings()
    nid = store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u"))
    store.set_setting("active_node_id", str(nid))
    state = _State(store, settings, _FakeSup("working"))
    loop = LivenessLoop(state, real_through=lambda *a, **k: (False, None, None, None))   # throwaway xray
    loop._probe_active()
    assert store.get_health(nid).fail_count == 1 and store.get_health(nid).last_real_ok is False
    loop._probe_active()
    assert store.get_health(nid).fail_count == 2             # responsive hysteresis (B2)


def test_liveness_tick_records_failover_event():
    store, settings = _store(), Settings()
    nid = store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u"))
    store.set_setting("active_node_id", str(nid))
    state = _State(store, settings, _FakeSup("working"))
    loop = LivenessLoop(state, real_through=lambda *a, **k: (True, 7, "9.9.9.9", None),
                        failover_run=lambda st, now: 42)
    loop._tick()
    assert any(e["kind"] == "failover" and "42" in e["detail"] for e in conn_events.recent(store))


# --- C1: uplink probe -------------------------------------------------------

class _OkSock:
    def close(self):
        pass


def test_uplink_up_true_false():
    assert netcheck.uplink_up(connect=lambda addr, to: _OkSock()) is True
    def boom(addr, to):
        raise OSError("unreachable")
    assert netcheck.uplink_up(connect=boom) is False


def test_network_status_uplink_injected():
    store = _store()
    assert netcheck.network_status(store, Settings(), uplink_check=lambda: True)["uplink"] is True
    assert netcheck.network_status(store, Settings())["uplink"] is None   # default: unknown


# --- N1 + N2: wan_blocked flag + event log on /network ----------------------

def _client(settings, stub_xray):
    settings.xray_bin = stub_xray
    c = TestClient(create_app(settings, state=build_state(settings, net=DryRunBackend())))
    c.post("/api/setup", json={"username": "admin", "password": "s3cret12"})
    return c, {"X-CSRF-Token": c.get("/api/csrf").json()["csrf"]}


def test_network_payload_has_wan_blocked_and_events(settings, stub_xray):
    c, h = _client(settings, stub_xray)
    c.put("/api/network", json={"kill_switch_enabled": True}, headers=h)
    body = c.get("/api/network").json()
    assert body["status"]["wan_blocked"] is True            # kill-switch on + tunnel down (N1)
    assert body["status"]["uplink"] is None                 # DryRun → uplink unknown
    assert any(e["kind"] == "kill-switch" for e in body["events"])   # event recorded (N2)
