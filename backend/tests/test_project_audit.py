"""Tests for the 2026-06-10 project audit fixes (B1-B9) and features N2/N4/N5."""
import time
import pytest
from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.net_control.plan import NetPlan
from pi_gw_panel.net_control.render import render_nft6
from pi_gw_panel.net_control.dnsmasq_supervisor import DnsmasqSupervisor
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.models import Node
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.health.monitor import HealthMonitor
from pi_gw_panel.health.probe import _parse_egress_ip
from pi_gw_panel.stats.history import TrafficHistory, TrafficRecorder
from pi_gw_panel.subs.fetcher import fetch
from pi_gw_panel.subs.parsers import safe_port, base64_vless, json_nodes, clash_yaml
from pi_gw_panel.xray_supervisor.supervisor import XraySupervisor


def _client(settings, stub_xray):
    settings.xray_bin = stub_xray
    app = create_app(settings, state=build_state(settings, net=DryRunBackend()))
    return TestClient(app)


def _login(c):
    c.post("/api/setup", json={"username": "admin", "password": "changeme123"})
    return c.get("/api/csrf").json()["csrf"]


def _store(settings) -> NodeStore:
    conn = connect(settings.db_path, check_same_thread=False)
    init_schema(conn)
    return NodeStore(conn)


# --- B1: xray reload waits for readiness ---

def test_reload_polls_ready_check(settings, stub_xray, tmp_path):
    cfg = tmp_path / "xray.json"
    cfg.write_text("{}")
    calls = []

    def ready():
        calls.append(1)
        return len(calls) >= 3

    sup = XraySupervisor(stub_xray, str(cfg), ready_check=ready)
    try:
        sup.reload()
        assert len(calls) >= 3            # polled until the check passed
    finally:
        sup.stop()


def test_reload_without_ready_check_does_not_wait(settings, stub_xray, tmp_path):
    cfg = tmp_path / "xray.json"
    cfg.write_text("{}")
    sup = XraySupervisor(stub_xray, str(cfg))
    t0 = time.monotonic()
    try:
        sup.reload()
        assert time.monotonic() - t0 < 1.0
    finally:
        sup.stop()


# --- B2: dnsmasq config is written atomically ---

def test_dnsmasq_apply_atomic(tmp_path):
    conf = tmp_path / "dnsmasq.conf"
    spawned = []

    class _P:
        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        pid = 1

    sup = DnsmasqSupervisor("dnsmasq", str(conf), popen=lambda cmd: (spawned.append(cmd), _P())[1])
    sup.apply("interface=eth0.2\n")
    assert conf.read_text() == "interface=eth0.2\n"
    assert not (tmp_path / "dnsmasq.conf.tmp").exists()   # temp renamed away
    assert spawned                                         # (re)started on change


# --- B3: v6 forward-drop whenever the tunnel is up, regardless of kill-switch ---

def _plan(**kw) -> NetPlan:
    base = dict(segment_iface="eth0.2", segment_ip="192.168.10.2", mgmt_iface="eth0",
                mgmt_ip="192.168.1.120", dhcp_start="192.168.10.30", dhcp_end="192.168.10.200",
                dhcp_lease="12h", client_dns="1.1.1.1", tproxy_port=52345, fwmark=0x40,
                egress_mark=0x80, table=100)
    base.update(kw)
    return NetPlan(**base)


def test_nft6_drop_when_tunnel_up_ipv6_off_kill_off():
    out = render_nft6(_plan(ipv6_enabled=False, kill_switch=False), tunnel_up=True)
    assert "chain forward" in out and "drop" in out
    assert "tproxy" not in out                       # no v6 tproxy when the v6 tunnel is off


def test_nft6_empty_when_tunnel_down_kill_off():
    assert render_nft6(_plan(ipv6_enabled=False, kill_switch=False), tunnel_up=False) == ""


# --- B4: node port validation (parsers + API schema) ---

def test_safe_port():
    assert safe_port("443") == 443
    assert safe_port(None) == 443                    # default
    assert safe_port("") == 443
    assert safe_port("0") is None
    assert safe_port("65536") is None
    assert safe_port("abc") is None
    assert safe_port(-5) is None


def test_parsers_skip_bad_ports():
    bad = "vless://u@h:99999?security=tls#x\nvless://u@h:443?security=tls#ok"
    nodes = base64_vless.parse(bad)
    assert [n.port for n in nodes] == [443]
    jnodes = json_nodes.parse('[{"address": "a", "port": "junk", "uuid": "u"},'
                              ' {"address": "b", "port": 8443, "uuid": "u"}]')
    assert [n.port for n in jnodes] == [8443]
    cy = ("proxies:\n"
          "  - {type: vless, name: bad, server: a, port: 700000, uuid: u}\n"
          "  - {type: vless, name: ok, server: b, port: 443, uuid: u}\n")
    cnodes = clash_yaml.parse(cy)
    assert [n.port for n in cnodes] == [443]


def test_api_rejects_out_of_range_port(settings, stub_xray):
    c = _client(settings, stub_xray)
    tok = _login(c)
    body = {"name": "n", "address": "1.2.3.4", "port": 0, "uuid": "u"}
    assert c.post("/api/nodes", json=body, headers={"X-CSRF-Token": tok}).status_code == 422
    body["port"] = 70000
    assert c.post("/api/nodes", json=body, headers={"X-CSRF-Token": tok}).status_code == 422


# --- B5: egress IP extraction only accepts real IP literals ---

def test_parse_egress_ip_strict():
    assert _parse_egress_ip('{"ip": "203.0.113.9"}') == "203.0.113.9"
    assert _parse_egress_ip("ip=2001:db8::1\nts=0") == "2001:db8::1"
    assert _parse_egress_ip("198.51.100.7") == "198.51.100.7"
    assert _parse_egress_ip("deadbeef") is None       # hex-ish junk used to pass
    assert _parse_egress_ip('{"ip": "not-an-ip"}') is None
    assert _parse_egress_ip("<html>error</html>") is None


# --- B6: subscription fetch refuses loopback targets ---

def test_fetch_rejects_loopback():
    for url in ("http://127.0.0.1:9/sub", "http://localhost/sub", "http://[::1]:8080/sub"):
        with pytest.raises(ValueError, match="loopback"):
            fetch(url, {}, {}, proxy=None)


# --- B7: the dev session secret never reaches SessionMiddleware ---

def test_session_secret_never_dev(settings, stub_xray):
    settings.xray_bin = stub_xray
    app = create_app(settings, state=build_state(settings, net=DryRunBackend()))
    session_mw = [m for m in app.user_middleware if "Session" in m.cls.__name__]
    assert session_mw, "SessionMiddleware not registered"
    assert session_mw[0].kwargs["secret_key"] != "dev-insecure-secret"


# --- B8: per-IP login rate limit ---

def test_login_rate_limit_locks_after_five_fails(settings, stub_xray):
    c = _client(settings, stub_xray)
    c.post("/api/setup", json={"username": "admin", "password": "changeme123"})
    c.post("/api/logout")
    for _ in range(4):
        assert c.post("/api/login", json={"username": "admin", "password": "no"}).status_code == 401
    # 5th failure trips the lockout; the next attempt is refused outright
    assert c.post("/api/login", json={"username": "admin", "password": "no"}).status_code == 401
    r = c.post("/api/login", json={"username": "admin", "password": "changeme123"})
    assert r.status_code == 429


def test_login_lockout_duration_configurable(settings, stub_xray):
    settings.login_lockout_sec = 0          # expires immediately
    c = _client(settings, stub_xray)
    c.post("/api/setup", json={"username": "admin", "password": "changeme123"})
    c.post("/api/logout")
    for _ in range(5):
        c.post("/api/login", json={"username": "admin", "password": "no"})
    r = c.post("/api/login", json={"username": "admin", "password": "changeme123"})
    assert r.status_code == 200             # zero-length lockout already over


# --- N2: mutation audit log ---

def test_audit_log_records_mutations(settings, stub_xray):
    c = _client(settings, stub_xray)
    tok = _login(c)
    body = {"name": "n1", "address": "1.2.3.4", "port": 47000, "uuid": "u-1"}
    assert c.post("/api/nodes", json=body, headers={"X-CSRF-Token": tok}).status_code == 200
    entries = c.get("/api/audit").json()
    paths = [(e["method"], e["path"]) for e in entries]
    assert ("POST", "/api/nodes") in paths
    node_entry = next(e for e in entries if e["path"] == "/api/nodes")
    assert node_entry["actor"] == "user:admin"
    assert node_entry["status"] == 200
    # GETs and failed mutations are not recorded
    assert all(e["method"] != "GET" for e in entries)


def test_audit_log_failed_mutations_not_recorded(settings, stub_xray):
    c = _client(settings, stub_xray)
    tok = _login(c)
    bad = {"name": "n", "address": "1.2.3.4", "port": 0, "uuid": "u"}
    c.post("/api/nodes", json=bad, headers={"X-CSRF-Token": tok})     # 422
    assert all(e["path"] != "/api/nodes" for e in c.get("/api/audit").json())


def test_audit_log_capped(settings):
    store = _store(settings)
    store._AUDIT_CAP = 5
    for i in range(8):
        store.add_audit(i, "user:t", "POST", f"/api/x{i}", 200)
    entries = store.list_audit(100)
    assert len(entries) == 5
    assert entries[0]["path"] == "/api/x7"            # newest first, oldest pruned


# --- N4: durable per-minute traffic history ---

def test_traffic_minutes_upsert_and_retention(settings):
    store = _store(settings)
    store.add_traffic_minute(1000, 600, 60)
    store.add_traffic_minute(1000, 400, 40)           # additive on conflict
    rows = store.traffic_minutes(since_min=0)
    assert rows == [{"ts_min": 1000, "up_bytes": 1000, "down_bytes": 100}]
    # a sample far in the future prunes anything beyond the retention window
    store.add_traffic_minute(1000 + store._TRAFFIC_RETENTION_MIN + 1, 1, 1)
    assert all(r["ts_min"] != 1000 for r in store.traffic_minutes(since_min=0))


def test_recorder_minute_rollover(settings):
    class _Sampler:
        totals = {}

        def sample(self):
            return {}

    sampler = _Sampler()
    clock = {"t": 0.0}
    minutes = []
    rec = TrafficRecorder(sampler=sampler, history=TrafficHistory(maxlen=10),
                          stats_enabled=lambda: True, interval_ms=lambda: 1000,
                          clock=lambda: clock["t"],
                          on_minute=lambda m, u, d: minutes.append((m, u, d)))
    sampler.totals = {"proxy": {"up": 100, "down": 0}}
    rec.record_sample({})                              # first sample: baseline only
    clock["t"] = 10.0
    sampler.totals = {"proxy": {"up": 250, "down": 30}}
    rec.record_sample({})                              # +150/+30 into minute 0
    clock["t"] = 70.0
    sampler.totals = {"proxy": {"up": 300, "down": 30}}
    rec.record_sample({})                              # minute rolls → flush minute 0
    assert minutes == [(0, 150, 30)]
    rec.flush_minute()                                 # shutdown flush persists minute 1
    assert minutes[-1] == (1, 50, 0)


def test_traffic_history_long_window_served_from_db(settings, stub_xray):
    c = _client(settings, stub_xray)
    _login(c)
    state = c.app.state.app_state
    now_min = int(time.time() // 60)
    state.store.add_traffic_minute(now_min - 10, 60_000, 6_000)   # bytes over one minute
    r = c.get("/api/traffic/history?window_sec=86400&max_points=500")
    assert r.status_code == 200
    doc = r.json()
    assert doc["interval_ms"] == 60000
    target = [s for s in doc["samples"] if s[1] == round(60_000 * 8 / 60)]
    assert target, f"expected a {round(60_000 * 8 / 60)} bit/s sample, got {doc['samples'][:3]}"


def test_traffic_history_short_window_unchanged(settings, stub_xray):
    c = _client(settings, stub_xray)
    _login(c)
    r = c.get("/api/traffic/history?window_sec=600")
    assert r.status_code == 200
    assert r.json()["interval_ms"] != 60000            # still the live-ring cadence


# --- N5: adaptive probe backoff for dead nodes ---

class _St:
    def __init__(self, store, settings):
        self.store = store
        self.settings = settings


def test_monitor_backoff_skips_dead_nodes(settings):
    st = _St(_store(settings), settings)
    nid = st.store.add_node(Node(id=None, name="dead", address="1.1.1.1", port=443, uuid="u"))
    probes = []
    mon = HealthMonitor(st, now_iso=lambda: "t",
                        tcp_ping=lambda a, p: (probes.append(1), (False, None))[1],
                        http_ping=lambda a, p, s: (False, None),
                        real_request=lambda *_: (False, None, None, None))
    mon.run_once()                  # probed, streak 1 → skip next 1 sweep
    assert len(probes) == 1
    mon.run_once()                  # skipped
    assert len(probes) == 1
    mon.run_once()                  # probed, streak 2 → skip next 3
    assert len(probes) == 2
    for _ in range(3):
        mon.run_once()              # skipped ×3
    assert len(probes) == 2
    mon.run_once()                  # probed again
    assert len(probes) == 3
    assert st.store.get_health(nid) is not None        # health row survives the skips


def test_monitor_backoff_resets_on_success(settings):
    st = _St(_store(settings), settings)
    st.store.add_node(Node(id=None, name="flaky", address="1.1.1.1", port=443, uuid="u"))
    alive = {"v": False}
    probes = []
    mon = HealthMonitor(st, now_iso=lambda: "t",
                        tcp_ping=lambda a, p: (probes.append(1), (alive["v"], 5 if alive["v"] else None))[1],
                        http_ping=lambda a, p, s: (alive["v"], 9 if alive["v"] else None),
                        real_request=lambda *_: (False, None, None, None))
    mon.run_once()                  # dead → backoff starts
    mon.run_once()                  # skipped
    alive["v"] = True
    mon.run_once()                  # probed (skip expired), alive → backoff cleared
    n_after_recovery = len(probes)
    mon.run_once()                  # probed again immediately (no skip)
    assert len(probes) == n_after_recovery + 1


def test_monitor_never_skips_active_node(settings):
    st = _St(_store(settings), settings)
    nid = st.store.add_node(Node(id=None, name="a", address="1.1.1.1", port=443, uuid="u"))
    st.store.set_setting("active_node_id", str(nid))
    st.store.set_setting("tunneled_fetch", "0")        # skip the real probe path
    probes = []
    mon = HealthMonitor(st, now_iso=lambda: "t",
                        tcp_ping=lambda a, p: (probes.append(1), (False, None))[1],
                        http_ping=lambda a, p, s: (False, None),
                        real_request=lambda *_: (False, None, None, None))
    for _ in range(4):
        mon.run_once()
    assert len(probes) == 4                            # active node probed every sweep
