import socket
import ssl
from datetime import UTC, datetime

from pi_gw_panel.controller import ApplyResult
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.health import failover, probe, selection
from pi_gw_panel.health.monitor import HealthMonitor
from pi_gw_panel.models import Node, NodeHealth, Subscription
from pi_gw_panel.nodes.store import NodeStore
from conftest import _client, _login


# --- probe.http_ping (TLS-handshake reachability) ---
def test_http_ping_ok_measures_latency():
    class _Conn:
        def close(self): pass
    seen = []
    def fake_connect(addr, timeout):
        seen.append((addr, timeout)); return _Conn()
    # three reads: the deadline is armed before the lookup, the dial is timed after it, and the
    # handshake is measured at the end. A literal address costs no lookup time, so the whole
    # 5 s budget is still on the dial.
    clock = iter([1.0, 1.0, 1.25])
    ok, ms = probe.http_ping("1.2.3.4", 443, "sni.example",
                             connect=fake_connect, clock=lambda: next(clock))
    assert ok is True and ms == 250
    assert seen == [(("1.2.3.4", 443), 5.0)]


def test_http_ping_fail_returns_none():
    def boom(addr, timeout): raise OSError("handshake failed")
    ok, ms = probe.http_ping("1.2.3.4", 443, "sni", connect=boom)
    assert ok is False and ms is None


# --- /api/probe/{tcp,http} sweeps ---
def _add(c, tok, name):
    body = {"name": name, "address": "1.2.3.4", "port": 443, "uuid": f"u-{name}",
            "sni": "x", "public_key": "PK", "short_id": "ab"}
    return c.post("/api/nodes", json=body, headers={"X-CSRF-Token": tok}).json()["id"]


def test_probe_tcp_sweep_updates_all_nodes(settings, stub_xray, monkeypatch):
    monkeypatch.setattr(probe, "tcp_ping", lambda a, p, **k: (True, 12))
    c = _client(settings, stub_xray); tok = _login(c)
    n1, n2 = _add(c, tok, "a"), _add(c, tok, "b")
    r = c.post("/api/probe/tcp", headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    h = {x["node_id"]: x for x in r.json()}
    assert h[n1]["last_tcp_ok"] is True and h[n1]["last_tcp_ms"] == 12
    assert h[n2]["last_tcp_ok"] is True


def test_probe_http_sweep_updates_http_fields(settings, stub_xray, monkeypatch):
    monkeypatch.setattr(probe, "http_ping", lambda a, p, sni, **k: (True, 34))
    c = _client(settings, stub_xray); tok = _login(c)
    nid = _add(c, tok, "a")
    r = c.post("/api/probe/http", headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    h = {x["node_id"]: x for x in r.json()}
    assert h[nid]["last_http_ok"] is True and h[nid]["last_http_ms"] == 34


# --- per-node real-through-node probe (the "T" button) ---
def test_probe_outbound_xhttp_and_reality():
    from pi_gw_panel.models import Node
    x = Node(id=1, name="x", address="a", port=443, uuid="u", transport="xhttp", network="xhttp",
             security="tls", sni="s", path="/p", host="h", mode="stream-up", alpn="h2,http/1.1")
    sx = probe._probe_outbound(x)["streamSettings"]
    assert sx["network"] == "xhttp" and sx["security"] == "tls"
    assert sx["xhttpSettings"]["path"] == "/p" and sx["tlsSettings"]["alpn"] == ["h2", "http/1.1"]
    r = Node(id=2, name="r", address="b", port=443, uuid="u2", transport="vision", network="tcp",
             security="reality", sni="t", public_key="PK", short_id="sid", flow="xtls-rprx-vision")
    o = probe._probe_outbound(r)
    assert o["streamSettings"]["realitySettings"]["publicKey"] == "PK"
    assert o["settings"]["vnext"][0]["users"][0]["flow"] == "xtls-rprx-vision"


def test_real_through_node_spawns_and_probes(monkeypatch):
    import json as _j
    from pi_gw_panel.models import Node
    # a routable public literal: probes now refuse private/loopback/unresolvable endpoints
    n = Node(id=1, name="n", address="1.2.3.7", port=443, uuid="u", transport="vision",
             network="tcp", security="reality", sni="s", public_key="PK", short_id="x")
    spawned = []
    class _Proc:
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout=None): pass
    def fake_spawn(path):
        spawned.append(_j.load(open(path))); return _Proc()
    monkeypatch.setattr(probe, "real_request", lambda proxy, url, timeout=5.0: (True, 200, 77, "9.9.9.9"))
    ok, ms, egress, egress6 = probe.real_through_node(n, "xray", "https://probe",
                                                      spawn=fake_spawn, wait_ready=lambda p: None)
    assert ok is True and ms == 77 and egress == "9.9.9.9" and egress6 is None
    cfg = spawned[0]
    assert cfg["inbounds"][0]["protocol"] == "http"
    assert cfg["outbounds"][0]["settings"]["vnext"][0]["address"] == "1.2.3.7"


def test_probe_node_endpoint_runs_all_three(settings, stub_xray, monkeypatch):
    monkeypatch.setattr(probe, "tcp_ping", lambda a, p, **k: (True, 5))
    monkeypatch.setattr(probe, "http_ping", lambda a, p, sni, **k: (True, 9))
    monkeypatch.setattr(probe, "real_through_node", lambda node, xb, url, **k: (True, 42, "9.9.9.9", None))
    c = _client(settings, stub_xray); tok = _login(c)
    nid = _add(c, tok, "a")
    r = c.post(f"/api/nodes/{nid}/probe", headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    h = r.json()
    assert h["last_tcp_ms"] == 5 and h["last_http_ms"] == 9 and h["last_real_ms"] == 42 and h["egress_ip"] == "9.9.9.9"


def test_probe_needs_csrf(settings, stub_xray):
    c = _client(settings, stub_xray); _login(c)
    assert c.post("/api/probe/tcp").status_code == 403


# --- the sweep and a node the operator hosts on their own LAN ------------------------------
#
# The SSRF guard on the probes is there so a subscription feed cannot aim them at internal
# services. Applied to every node regardless of origin it also refused the operator's own
# same-LAN node, which then never got a probe result at all — and `selection._alive` needs one
# of the three, so auto-failover (which asks for an alive candidate) could never pick it.
# Provenance is the discriminator, and it already exists: a feed-imported node carries a
# `subscription_id`, a hand-added one does not.


class _SweepState:
    def __init__(self, store, settings):
        self.store = store
        self.settings = settings
        self.xray_bin = settings.xray_bin


class _NoTls:
    """The default `http_ping` connect path, with the TLS layer stubbed out — the socket still
    goes through `socket.create_connection`, which is what the assertions watch."""
    check_hostname = True
    verify_mode = None

    def wrap_socket(self, sock, server_hostname=None):
        return sock


def test_the_sweep_probes_an_operator_added_lan_node_but_never_a_feed_one(settings, monkeypatch):
    """Nothing here is stubbed above the socket: the production `tcp_ping`/`http_ping` do the
    work, so what is under test is the whole chain — monitor → probe → resolver → dial.
    (`tcp_ping` takes its dialler as a default argument, so it is handed one explicitly rather
    than through the module: patching `socket` alone would leave it dialling the real LAN.)"""
    conn = connect(settings.db_path, check_same_thread=False)
    init_schema(conn)
    store = NodeStore(conn)
    sub = store.add_subscription(Subscription(id=None, name="feed", url="https://feed.example/s"))
    mine = store.add_node(Node(id=None, name="lan", address="192.168.1.10", port=443, uuid="u1"))
    theirs = store.add_node(Node(id=None, name="feed-lan", address="192.168.1.11", port=443,
                                 uuid="u2", subscription_id=sub))

    class _Conn:
        def close(self): pass

    dialled = []

    def dial(addr, to):
        dialled.append(addr[0])
        return _Conn()

    def tcp_ping(address, port, allow_private=False):
        return probe.tcp_ping(address, port, connect=dial, allow_private=allow_private)

    monkeypatch.setattr(socket, "create_connection", dial)      # the handshake probe's dialler
    monkeypatch.setattr(ssl, "create_default_context", lambda: _NoTls())

    HealthMonitor(_SweepState(store, settings), tcp_ping=tcp_ping,
                  real_request=lambda *_a, **_k: (False, None, None, None)).run_once()

    # the operator's node answered both direct probes; the feed's identical-looking one was
    # never dialled at all — that half is the SSRF guard and must never regress
    assert dialled == ["192.168.1.10", "192.168.1.10"]      # tcp probe, then the handshake
    assert (store.get_health(mine).last_tcp_ok, store.get_health(mine).last_http_ok) == (True, True)
    assert (store.get_health(theirs).last_tcp_ok,
            store.get_health(theirs).last_http_ok) == (False, False)

    # …and being probed at all is what makes it selectable: failover asks for an alive candidate
    health = {h.node_id: h for h in store.list_health()}
    nodes = store.list_nodes()
    assert [n.id for n in selection.ranked_nodes(nodes, health, require_alive=True)] == [mine]
    assert selection.best_node(nodes, health, require_alive=True).id == mine


# --- …and selectable is not promotable: the preflight had the last word --------------------
#
# `failover.run` promotes nothing whose real-request preflight failed, and that preflight
# resolves the candidate's endpoint before it stands anything up. While it resolved strictly,
# the operator's own LAN node was swept, marked alive and ranked (above) — and then refused on
# every evaluation, so the panel offered a candidate it could never switch to. That is the
# defect, and it is about the OUTCOME, so these assert the promotion rather than the probe.
#
# Both drive the PRODUCTION `real_through_node`: only the xray process and the request through
# it are stubbed, so the guard is the only thing deciding, and the adapter forwards `**k`
# verbatim — stop passing provenance in `failover.run` and the promotion test goes red.


def _ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


class _NoProc:
    """A throwaway xray that never runs; the preflight decides before spawn either way."""

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        pass


class _FailoverState(_SweepState):
    supervisor = None       # only ever handed to the stubbed apply_fn
    net = None


def _preflight(spawned, monkeypatch):
    monkeypatch.setattr(probe, "real_request",
                        lambda proxy, url, timeout=None: (True, 200, 7, "203.0.113.9"))

    def real_through(node, xray_bin, probe_url, **k):
        return probe.real_through_node(
            node, xray_bin, probe_url, wait_ready=lambda _port: None,
            spawn=lambda _path: spawned.append(node.address) or _NoProc(), **k)

    return real_through


def _one_lan_standby(store, address, subscription_id=None):
    """An active node that has failed its way past the hysteresis, and one standby on the LAN."""
    now = 100_000.0
    active = store.add_node(Node(id=None, name="wan", address="1.1.1.1", port=443, uuid="u1"))
    standby = store.add_node(Node(id=None, name="lan", address=address, port=443, uuid="u2",
                                  subscription_id=subscription_id))
    store.set_setting("active_node_id", str(active))
    store.upsert_health(NodeHealth(node_id=active, last_real_ok=False, fail_count=3,
                                   checked_at=_ts(now)))
    store.upsert_health(NodeHealth(node_id=standby, last_tcp_ok=True, checked_at=_ts(now)))
    return now, standby


def _failover_store(settings):
    conn = connect(settings.db_path, check_same_thread=False)
    init_schema(conn)
    return NodeStore(conn)


def test_failover_promotes_an_operator_added_node_on_the_lan(settings, monkeypatch):
    store = _failover_store(settings)
    now, standby = _one_lan_standby(store, "192.168.1.10")
    applied, spawned = [], []

    def fake_apply(node, *_a, store=None, **_k):
        applied.append(node.id)
        store.set_setting("active_node_id", str(node.id))
        return ApplyResult(ok=True)

    assert failover.run(_FailoverState(store, settings), now, apply_fn=fake_apply,
                        real_through=_preflight(spawned, monkeypatch)) == standby
    assert applied == [standby] and spawned == ["192.168.1.10"]
    assert store.get_health(standby).last_real_ok is True


def test_failover_still_refuses_to_preflight_a_feed_imported_lan_node(settings, monkeypatch):
    """The direction that must never regress. Identical to the promotion above but for one
    field — the node carries a `subscription_id`, so a feed put it there. The preflight refuses
    the address, no xray is stood up pointing at it, and nothing is promoted."""
    store = _failover_store(settings)
    sub = store.add_subscription(Subscription(id=None, name="f", url="https://feed.example/s"))
    now, standby = _one_lan_standby(store, "192.168.1.10", subscription_id=sub)
    applied, spawned = [], []

    def fake_apply(node, *_a, **_k):
        applied.append(node.id)
        return ApplyResult(ok=True)

    assert failover.run(_FailoverState(store, settings), now, apply_fn=fake_apply,
                        real_through=_preflight(spawned, monkeypatch)) is None
    assert applied == [] and spawned == []
    assert store.get_health(standby).last_real_ok is False
    assert store.get_setting("active_node_id") != str(standby)
