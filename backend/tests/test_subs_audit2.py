"""Hardening of the subscription boundary: everything here comes from a remote, operator-
supplied feed and lands in a root-privileged gateway's live config, so each test pins one
defect that let feed content decide something it must not.
"""
import asyncio
import http.server
import socket
import threading
import time
import urllib.parse

import pytest
import yaml

from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.config import Settings
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.models import Node, Subscription
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.state import build_state
from pi_gw_panel.subs import fetcher, service
from pi_gw_panel.subs.parsers import clamp_node_fields
from pi_gw_panel.subs.parsers.clash_yaml import MAX_YAML_DEPTH
from pi_gw_panel.subs.parsers.dispatch import parse_subscription
from pi_gw_panel.subs.reconcile import reconcile
from pi_gw_panel.subs.scheduler import SubScheduler
from pi_gw_panel.xray_config.builder import build_config


def _store(settings):
    conn = connect(settings.db_path, check_same_thread=False)
    init_schema(conn)
    return NodeStore(conn)


def _proxy_outbound(cfg):
    return next(o for o in cfg["outbounds"] if o["tag"] == "proxy")


# --- H5: a feed must never be able to turn the tunnel's security off ---
def test_xhttp_feed_offering_security_none_is_downgraded_not_honoured():
    """`security=none` on an xhttp node is the exploitable shape (the Vision variant already
    fails closed because normalize() clears the flow). It must never reach streamSettings."""
    uri = "vless://user-x@1.2.3.4:443?type=xhttp&security=none&sni=cdn.example&path=/dl#node"
    node = parse_subscription(uri)[0]
    assert node.transport == "xhttp" and node.network == "xhttp"
    assert node.security == "tls"                       # not "none"
    stream = _proxy_outbound(build_config(node, Settings()))["streamSettings"]
    assert stream["security"] == "tls"
    assert "tlsSettings" in stream


def test_reality_feed_security_none_with_key_falls_back_to_reality():
    uri = "vless://user-x@1.2.3.4:443?security=none&pbk=PK&sid=SID&sni=hilex.se#node"
    node = parse_subscription(uri)[0]
    assert node.security == "reality"


def test_normalize_rejects_any_unknown_security_value():
    node = Node(id=None, name="n", address="a", port=443, uuid="u", security="none")
    assert node.security == "tls"
    node.security = "NONE"
    node.normalize()
    assert node.security == "tls"


def test_builder_fails_closed_on_an_unknown_security_value():
    """Belt and braces: if anything ever bypasses normalize(), the config render refuses
    rather than emitting a plaintext VLESS outbound."""
    node = Node(id=None, name="n", address="a", port=443, uuid="u")
    node.security = "none"                              # bypass normalize deliberately
    with pytest.raises(ValueError, match="security"):
        build_config(node, Settings())


# --- F6-4: a truncated feed is a failed fetch, never a shorter node list ---
class _TruncatingHandler(http.server.BaseHTTPRequestHandler):
    """Declares more bytes than it sends, then closes — http.client returns the prefix
    instead of raising, so without the length check reconcile would delete the rest."""

    body = b"vless://u@1.1.1.1:443?pbk=PK&sid=S#a\nvless://u@2.2.2.2:443?pbk=PK&sid=S#b\n"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(self.body) + 64))   # lies
        self.end_headers()
        self.wfile.write(self.body[:20])

    def log_message(self, *args):
        pass


def _serve(handler):
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/sub"


def test_truncated_body_is_a_failed_fetch(monkeypatch):
    monkeypatch.setattr(fetcher, "ALLOW_LOOPBACK", True)
    server, url = _serve(_TruncatingHandler)
    try:
        with pytest.raises(ValueError, match="truncated"):
            fetcher._http_get(url, {}, None, 5.0)
    finally:
        server.shutdown()


def test_truncated_refresh_deletes_zero_nodes(monkeypatch, settings):
    monkeypatch.setattr(fetcher, "ALLOW_LOOPBACK", True)
    store = _store(settings)
    server, url = _serve(_TruncatingHandler)
    try:
        sid = store.add_subscription(Subscription(id=None, name="s", url=url))
        for i in range(3):
            store.add_node(Node(id=None, name=f"n{i}", address=f"9.9.9.{i}", port=443,
                                uuid=f"u{i}", subscription_id=sid))

        class FakeState:
            store = None
            supervisor = type("Sup", (), {"status": lambda self: {"running": False}})()

        state = FakeState()
        state.store = store
        state.settings = settings
        result = service.refresh(state, store.get_subscription(sid))
        assert result["ok"] is False
        assert len(store.list_nodes_for_sub(sid)) == 3        # nothing deleted on a bad fetch
    finally:
        server.shutdown()


# --- F6-3: injected credentials must not follow a cross-origin redirect ---
def test_injected_headers_are_dropped_on_a_cross_origin_redirect(monkeypatch):
    seen = []

    def resolve(host, port, deadline):
        return {"one.example": "93.184.216.34", "two.example": "93.184.216.35"}[host]

    def request(parts, pinned_ip, headers, proxy, deadline):
        seen.append((parts.hostname, dict(headers)))
        if parts.hostname == "one.example":
            return 302, [("Location", "https://two.example/final")], b""
        return 200, [("Content-Type", "text/plain")], b"ok"

    monkeypatch.setattr(fetcher, "_resolve_public", resolve)
    monkeypatch.setattr(fetcher, "_request_once", request)
    injected = {"x-hwid": "SECRET-HWID", "Authorization": "Bearer TOKEN",
                "user-agent": "v2pi/1.0"}
    body, _headers = fetcher._http_get("https://one.example/start", injected, None, 5.0)
    assert body == "ok"
    first, second = seen[0][1], seen[1][1]
    assert first["x-hwid"] == "SECRET-HWID" and first["Authorization"] == "Bearer TOKEN"
    assert "x-hwid" not in second and "Authorization" not in second
    assert second["user-agent"] == "v2pi/1.0"          # non-credential headers still travel


def test_injected_headers_survive_a_same_origin_redirect(monkeypatch):
    seen = []

    def request(parts, pinned_ip, headers, proxy, deadline):
        seen.append(dict(headers))
        if len(seen) == 1:
            return 302, [("Location", "https://one.example/final")], b""
        return 200, [("Content-Type", "text/plain")], b"ok"

    monkeypatch.setattr(fetcher, "_resolve_public", lambda h, p, d: "93.184.216.34")
    monkeypatch.setattr(fetcher, "_request_once", request)
    fetcher._http_get("https://one.example/start", {"x-hwid": "SECRET"}, None, 5.0)
    assert seen[1]["x-hwid"] == "SECRET"


@pytest.mark.parametrize("start, location, travels", [
    ("http://one.example/s", "https://one.example/s", True),      # same-host TLS upgrade
    ("https://one.example/s", "http://one.example/s", False),     # downgrade
    ("https://one.example/s", "https://one.example:8443/s", False),
    ("https://one.example/s", "https://evil.example/s", False),
])
def test_credential_travel_rules(monkeypatch, start, location, travels):
    seen = []

    def request(parts, pinned_ip, headers, proxy, deadline):
        seen.append(dict(headers))
        if len(seen) == 1:
            return 302, [("Location", location)], b""
        return 200, [("Content-Type", "text/plain")], b"ok"

    monkeypatch.setattr(fetcher, "_resolve_public", lambda h, p, d: "93.184.216.34")
    monkeypatch.setattr(fetcher, "_request_once", request)
    fetcher._http_get(start, {"x-hwid": "SECRET"}, None, 5.0)
    assert ("x-hwid" in seen[1]) is travels


# --- F6-5: the operator's note is theirs, not the feed's ---
def test_operator_note_survives_a_refresh(settings):
    store = _store(settings)
    sid = store.add_subscription(Subscription(id=None, name="s", url="u"))
    nid = store.add_node(Node(id=None, name="A", address="1.1.1.1", port=443, uuid="ua",
                              note="paid until March", subscription_id=sid))
    reconcile(store, sid, [Node(id=None, name="A renamed", address="1.1.1.1", port=443,
                                uuid="ua")], active_node_id=None)
    assert store.get_node(nid).note == "paid until March"
    assert store.get_node(nid).name == "A renamed"      # feed-owned fields still update


# --- F6-12: feed strings stay bounded ---
def test_security_and_transport_are_length_clamped():
    node = Node(id=None, name="n", address="a", port=443, uuid="u")
    node.security = "s" * 900
    node.transport = "t" * 900
    clamp_node_fields(node)
    assert len(node.security) == 512 and len(node.transport) == 512


# --- F6-13: the response picks its decoder; only ones we know are allowed ---
@pytest.mark.parametrize("charset", ["idna", "punycode", "not-a-charset", "zip"])
def test_hostile_charset_does_not_escape_http_get(monkeypatch, charset):
    monkeypatch.setattr(fetcher, "_resolve_public", lambda h, p, d: "93.184.216.34")
    monkeypatch.setattr(fetcher, "_request_once", lambda *a, **k: (
        200, [("Content-Type", f"text/plain; charset={charset}")], "vless://ünïcode".encode()))
    body, _headers = fetcher._http_get("https://one.example/s", {}, None, 5.0)
    assert body == "vless://ünïcode"                   # decoded as utf-8, no UnicodeError


# --- F6-9: repeated response headers must survive ---
def test_every_set_cookie_is_kept_across_a_redirect(monkeypatch):
    seen = []

    def request(parts, pinned_ip, headers, proxy, deadline):
        seen.append(dict(headers))
        if len(seen) == 1:
            return 302, [("Set-Cookie", "a=1; Path=/"), ("Set-Cookie", "b=2; Path=/"),
                         ("Location", "https://one.example/final")], b""
        return 200, [("Content-Type", "text/plain")], b"ok"

    monkeypatch.setattr(fetcher, "_resolve_public", lambda h, p, d: "93.184.216.34")
    monkeypatch.setattr(fetcher, "_request_once", request)
    fetcher._http_get("https://one.example/start", {}, None, 5.0)
    assert "a=1" in seen[1]["Cookie"] and "b=2" in seen[1]["Cookie"]


class _DuplicateHeaderHandler(http.server.BaseHTTPRequestHandler):
    body = b"vless://u@1.1.1.1:443?pbk=PK&sid=S#a"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Set-Cookie", "a=1; Path=/")
        self.send_header("Set-Cookie", "b=2; Path=/")
        self.send_header("Subscription-Userinfo", "upload=10")
        self.send_header("Subscription-Userinfo", "download=20")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


def test_response_keeps_repeated_headers_off_the_wire(monkeypatch):
    """Collapsing the response into a dict drops all but one Set-Cookie /
    Subscription-Userinfo before anything downstream ever sees them."""
    monkeypatch.setattr(fetcher, "ALLOW_LOOPBACK", True)
    server, url = _serve(_DuplicateHeaderHandler)
    try:
        _body, headers = fetcher._http_get(url, {}, None, 5.0)
    finally:
        server.shutdown()
    assert fetcher._header_all(headers, "set-cookie") == ["a=1; Path=/", "b=2; Path=/"]
    assert fetcher._header_all(headers, "subscription-userinfo") == ["upload=10", "download=20"]


def test_repeated_subscription_userinfo_headers_are_all_read():
    sub = Subscription(id=1, name="s", url="u")
    service._apply_userinfo(sub, [("Subscription-Userinfo", "upload=10"),
                                  ("Subscription-Userinfo", "download=20; total=99")])
    assert sub.up_bytes == 10 and sub.down_bytes == 20 and sub.total_bytes == 99


# --- F6-2: the advertised wall deadline has to be real ---
def test_dribbling_server_cannot_outlive_the_wall_deadline(monkeypatch):
    """sock.settimeout() is a per-recv idle timer, so a server trickling one byte at a time
    resets it forever. The wall deadline must close the socket regardless."""
    ours, theirs = socket.socketpair()
    stop = threading.Event()

    def dribble():
        deadline = time.monotonic() + 4.0
        try:
            theirs.recv(4096)                          # swallow the request
            while not stop.is_set() and time.monotonic() < deadline:
                theirs.sendall(b"H")
                time.sleep(0.02)
        except OSError:
            pass
        finally:
            theirs.close()

    thread = threading.Thread(target=dribble, daemon=True)
    thread.start()
    monkeypatch.setattr(fetcher.socket, "create_connection", lambda *a, **k: ours)
    parts = urllib.parse.urlsplit("http://slow.example/sub")
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            fetcher._request_once(parts, "93.184.216.34", {}, None, time.monotonic() + 0.3)
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        thread.join(timeout=5)
        ours.close()
    assert elapsed < 3.0, f"deadline was not enforced (returned after {elapsed:.2f}s)"


def test_scheduler_abandons_a_wedged_refresh_and_queues_nothing_behind_it(settings, monkeypatch):
    """A wedged worker must not pin the tick — and, having been abandoned, must not then be
    *retried* while it is still running. Four repeats of one bad feed exhaust the four-worker
    pool, and every repeat only queues behind the same per-subscription refresh lock."""
    import pi_gw_panel.subs.scheduler as scheduler_module

    store = _store(settings)
    sid = store.add_subscription(Subscription(id=None, name="s", url="u", interval_sec=60))
    released = threading.Event()
    submits = []
    monkeypatch.setattr(scheduler_module, "REFRESH_TIMEOUT", 0.2)

    def wedged(state, sub):
        submits.append(sub.id)
        released.wait(10)
        return {"ok": True}

    monkeypatch.setattr(service, "refresh", wedged)
    sched = SubScheduler(type("S", (), {"store": store})())
    try:
        started = time.monotonic()
        sched.run_once(time.monotonic())
        assert time.monotonic() - started < 3.0          # abandoned, not awaited
        assert set(sched._inflight) == {sid}             # still accounted for as live work
        assert not sched._failures and not sched._retry_at   # no retry armed behind it
        later = time.monotonic() + 10_000                # long past the 60 s interval
        assert sched.due_subs(later) == []
        sched.run_once(later)
        assert submits == [sid], "a second refresh was submitted while the first was still live"
    finally:
        released.set()
        if sched._pool is not None:
            sched._pool.shutdown(wait=True)


def test_scheduler_bounds_the_whole_batch_by_one_deadline(settings, monkeypatch):
    """The per-future waits ran in series, so one tick could block for 4 × REFRESH_TIMEOUT."""
    import pi_gw_panel.subs.scheduler as scheduler_module

    store = _store(settings)
    for i in range(4):
        store.add_subscription(Subscription(id=None, name=f"s{i}", url="u", interval_sec=60))
    released = threading.Event()
    monkeypatch.setattr(scheduler_module, "REFRESH_TIMEOUT", 0.5)
    monkeypatch.setattr(service, "refresh", lambda state, sub: released.wait(10) or {"ok": True})
    sched = SubScheduler(type("S", (), {"store": store})())
    try:
        started = time.monotonic()
        sched.run_once(time.monotonic())
        elapsed = time.monotonic() - started
        assert elapsed < 1.2, f"tick took {elapsed:.2f}s — the deadline is per-future, not per-batch"
        assert len(sched._inflight) == 4
    finally:
        released.set()
        if sched._pool is not None:
            sched._pool.shutdown(wait=True)


def test_scheduler_shutdown_does_not_wait_on_a_wedged_refresh(settings, monkeypatch):
    """`stop()` joined the pool unconditionally, so one stuck worker held the panel's whole
    shutdown open for as long as it took."""
    import pi_gw_panel.subs.scheduler as scheduler_module

    store = _store(settings)
    store.add_subscription(Subscription(id=None, name="s", url="u", interval_sec=60))
    released = threading.Event()
    monkeypatch.setattr(scheduler_module, "REFRESH_TIMEOUT", 0.2)
    monkeypatch.setattr(scheduler_module, "SHUTDOWN_TIMEOUT", 0.3)
    monkeypatch.setattr(service, "refresh", lambda state, sub: released.wait(6) or {"ok": True})
    sched = SubScheduler(type("S", (), {"store": store})())

    async def drive():
        sched.run_once(time.monotonic())
        started = time.monotonic()
        await sched.stop()
        return time.monotonic() - started

    try:
        elapsed = asyncio.run(drive())
        assert elapsed < 3.0, f"shutdown waited {elapsed:.2f}s on a worker it had given up on"
    finally:
        released.set()


# --- F6-8: remote content on a timer must not silently move the live tunnel ---
def _fake_state(store, settings):
    supervisor = type("Sup", (), {"status": lambda self: {"running": False}})()
    return type("S", (), {"store": store, "settings": settings, "net": object(),
                          "supervisor": supervisor, "xray_bin": None})()


def test_auto_switch_can_be_turned_off(monkeypatch, settings):
    store = _store(settings)
    sid = store.add_subscription(Subscription(id=None, name="x", url="u"))
    old = store.add_node(Node(id=None, name="old", address="1.1.1.1", port=443, uuid="u",
                              subscription_id=sid, stale=True))
    new = store.add_node(Node(id=None, name="new", address="2.2.2.2", port=443, uuid="v",
                              subscription_id=sid))
    calls = []
    monkeypatch.setattr(service, "apply_node", lambda node, *a, **k: calls.append(node.id))
    store.set_setting("active_node_id", str(old))        # or _restart_active stands down anyway
    store.set_setting("subs_auto_switch", "0")
    service._restart_active(_fake_state(store, settings), old,
                            {"active_changed": False, "active_replacement": new})
    assert calls == []
    assert store.get_node(old) is not None               # live node left exactly where it was


def test_auto_switch_refuses_a_weaker_replacement(monkeypatch, settings):
    store = _store(settings)
    sid = store.add_subscription(Subscription(id=None, name="x", url="u"))
    old = store.add_node(Node(id=None, name="old", address="1.1.1.1", port=443, uuid="u",
                              security="reality", public_key="PK", subscription_id=sid, stale=True))
    weaker = store.add_node(Node(id=None, name="new", address="2.2.2.2", port=443, uuid="v",
                                 security="tls", subscription_id=sid))
    calls = []
    monkeypatch.setattr(service, "apply_node", lambda node, *a, **k: calls.append(node.id))
    store.set_setting("active_node_id", str(old))        # or _restart_active stands down anyway
    service._restart_active(_fake_state(store, settings), old,
                            {"active_changed": False, "active_replacement": weaker})
    assert calls == []
    assert store.get_node(old) is not None


# --- REVISE-1: the same ladder must gate an *in-place* rewrite of the active node ---
def _live_reality(sid, **over):
    fields = dict(name="live", address="1.1.1.1", port=443, uuid="u", sni="s", short_id="sid",
                  security="reality", public_key="PK", subscription_id=sid)
    return Node(id=None, **{**fields, **over})


def test_in_place_downgrade_of_the_active_node_is_refused(monkeypatch, settings):
    """The gate only ever consulted a *replacement* node. A feed re-advertising the same
    identity with `security` knocked down walked straight past it into the live tunnel."""
    store = _store(settings)
    sid = store.add_subscription(Subscription(id=None, name="x", url="u"))
    nid = store.add_node(_live_reality(sid))
    was = store.get_node(nid)                            # the pre-reconcile snapshot
    weakened = _live_reality(sid, security="tls", public_key="")
    weakened.id = nid
    store.update_node(weakened)                          # what reconcile has already written
    store.set_setting("active_node_id", str(nid))
    calls = []
    monkeypatch.setattr(service, "apply_node", lambda node, *a, **k: calls.append(node.id))
    result = service._restart_active(_fake_state(store, settings), nid,
                                     {"active_changed": True, "active_replacement": None}, was)
    assert calls == []
    assert result is not None and result.ok is False and "downgrade" in result.error


def test_in_place_upgrade_of_the_active_node_is_still_applied(monkeypatch, settings):
    """The refusal is one-directional: a feed moving the live tunnel tls → reality is exactly
    the unattended change the panel exists to pick up, and must keep being applied."""
    store = _store(settings)
    sid = store.add_subscription(Subscription(id=None, name="x", url="u"))
    nid = store.add_node(_live_reality(sid, security="tls", public_key=""))
    was = store.get_node(nid)
    upgraded = _live_reality(sid)
    upgraded.id = nid
    store.update_node(upgraded)
    store.set_setting("active_node_id", str(nid))
    calls = []
    monkeypatch.setattr(service, "apply_node", lambda node, *a, **k: calls.append(node.id))
    service._restart_active(_fake_state(store, settings), nid,
                            {"active_changed": True, "active_replacement": None}, was)
    assert calls == [nid]


def _refresh_with_feed(monkeypatch, store, settings, sid, parsed):
    monkeypatch.setattr(service, "fetch", lambda *a, **k: ("body", "direct", {}))
    monkeypatch.setattr(service, "parse_subscription", lambda body, limit=None: parsed)
    return service.refresh(_fake_state(store, settings), store.get_subscription(sid))


def test_scheduled_refresh_cannot_weaken_the_live_tunnel_in_place(monkeypatch, settings):
    """End-to-end shape of the attack: an unattended refresh whose feed flips the active
    node's Reality to plain TLS. The refresh must fail, the stored row must be put back, and
    nothing may be applied — leaving neither the tunnel nor the next Connect on the weaker mode."""
    store = _store(settings)
    sid = store.add_subscription(Subscription(id=None, name="x", url="https://93.184.216.34/s"))
    nid = store.add_node(_live_reality(sid))
    store.set_setting("active_node_id", str(nid))
    calls = []
    monkeypatch.setattr(service, "apply_node", lambda node, *a, **k: calls.append(node.id))
    result = _refresh_with_feed(monkeypatch, store, settings, sid,
                                [_live_reality(sid, security="tls", public_key="")])
    assert result["ok"] is False and "downgrade" in result["error"]
    assert calls == []
    assert store.get_node(nid).security == "reality"     # row restored, not parked weakened
    assert store.get_subscription(sid).last_status.startswith("error:")


def test_scheduled_refresh_still_applies_a_feed_driven_upgrade(monkeypatch, settings):
    store = _store(settings)
    sid = store.add_subscription(Subscription(id=None, name="x", url="https://93.184.216.34/s"))
    nid = store.add_node(_live_reality(sid, security="tls", public_key=""))
    store.set_setting("active_node_id", str(nid))
    calls = []
    monkeypatch.setattr(service, "apply_node", lambda node, *a, **k: calls.append(node.id))
    result = _refresh_with_feed(monkeypatch, store, settings, sid, [_live_reality(sid)])
    assert result["ok"] is True and calls == [nid]
    assert store.get_node(nid).security == "reality"


# --- F6-10: a bad subscription URL is reported when it is saved ---
def test_saving_a_private_subscription_url_is_rejected(settings, stub_xray):
    settings.xray_bin = stub_xray
    client = TestClient(create_app(settings, state=build_state(settings, net=DryRunBackend())))
    client.post("/api/setup", json={"username": "admin", "password": "changeme"})
    headers = {"X-CSRF-Token": client.get("/api/csrf").json()["csrf"]}
    for url in ("http://192.168.1.1/sub", "http://169.254.169.254/latest/meta-data"):
        assert client.post("/api/subs", json={"name": "s", "url": url},
                           headers=headers).status_code == 422
    ok = client.post("/api/subs", json={"name": "s", "url": "https://93.184.216.34/sub"},
                     headers=headers)
    assert ok.status_code == 200
    assert client.patch(f"/api/subs/{ok.json()['id']}", json={"url": "http://10.0.0.1/sub"},
                        headers=headers).status_code == 422


# --- F6-15: the loopback test seam must not open the whole private space ---
def test_allow_loopback_only_relaxes_loopback(monkeypatch):
    monkeypatch.setattr(fetcher, "ALLOW_LOOPBACK", True)
    assert fetcher._resolve_public("127.0.0.1", 80, time.monotonic() + 1) == "127.0.0.1"
    for internal in ("10.0.0.1", "192.168.1.1", "169.254.169.254", "::1"):
        if internal == "::1":
            assert fetcher._resolve_public(internal, 80, time.monotonic() + 1) == "::1"
            continue
        with pytest.raises(ValueError, match="non-public"):
            fetcher._resolve_public(internal, 80, time.monotonic() + 1)


# --- F6-16: a nesting bomb is a rejected feed, not a RecursionError ---
def test_deeply_nested_clash_yaml_is_rejected():
    body = "proxies:\n" + "  " + "[" * (MAX_YAML_DEPTH + 50) + "]" * (MAX_YAML_DEPTH + 50) + "\n"
    with pytest.raises(yaml.YAMLError):
        parse_subscription(body)


def test_ordinary_clash_yaml_still_parses():
    body = ("proxies:\n"
            "  - name: x1\n    type: vless\n    server: 1.2.3.4\n    port: 443\n"
            "    uuid: u\n    network: xhttp\n    servername: ya.ru\n"
            "    xhttp-opts:\n      path: /dl\n      headers:\n        Host: cdn.example\n")
    assert parse_subscription(body)[0].path == "/dl"


# --- F6-11: per-subscription bookkeeping is pruned when the subscription goes ---
def test_scheduler_forgets_deleted_subscriptions(settings):
    store = _store(settings)
    keep = store.add_subscription(Subscription(id=None, name="keep", url="u", interval_sec=60))
    drop = store.add_subscription(Subscription(id=None, name="drop", url="u", interval_sec=60))
    sched = SubScheduler(type("S", (), {"store": store})())
    now = time.monotonic()
    sched.due_subs(now)
    sched._last_run[keep] = sched._last_run[drop] = now
    sched._failures[drop] = 2
    service._refresh_lock(drop)
    store.delete_subscription(drop)
    sched.due_subs(time.monotonic())
    assert keep in sched._last_run and drop not in sched._last_run
    assert drop not in sched._failures
    assert drop not in service._REFRESH_LOCKS


# --- H4 (cycle 2): the refused downgrade must never be written, not written-then-restored ---
def test_reconcile_refuses_to_write_the_weakened_active_row(monkeypatch, settings):
    """Cycle 1 let reconcile commit the weakened row and undid it afterwards. Every store
    mutator commits, so that row was durable and readable the moment it landed — a concurrent
    manual apply could capture the downgrade, and a restore that itself failed parked it for
    the next Connect. The refusal has to happen at the write."""
    store = _store(settings)
    sid = store.add_subscription(Subscription(id=None, name="x", url="u"))
    nid = store.add_node(_live_reality(sid))
    writes = []
    original = store.update_node
    monkeypatch.setattr(
        store, "update_node",
        lambda node: (writes.append((node.id, node.security)), original(node))[-1])
    fresh = _live_reality(sid, address="9.9.9.9", name="fresh")
    counts = reconcile(store, sid, [_live_reality(sid, security="tls", public_key=""), fresh],
                       nid, None)
    assert (nid, "tls") not in writes, "the weakened row reached the store at all"
    assert store.get_node(nid).security == "reality"
    assert counts["active_downgrade"] == ("reality", "tls")
    assert counts["active_changed"] is False       # nothing to re-apply; the live config stands
    assert counts["added"] == 1 and counts["updated"] == 0


def test_refresh_reports_the_refusal_and_keeps_the_rest_of_the_merge(monkeypatch, settings):
    """End-to-end: the weak value never reaches the store (so it never needed restoring), the
    subscription goes red with an actionable status, and the nodes the same feed legitimately
    added still land — one hostile row must not block every other update forever."""
    store = _store(settings)
    sid = store.add_subscription(Subscription(id=None, name="x", url="https://93.184.216.34/s"))
    nid = store.add_node(_live_reality(sid))
    store.set_setting("active_node_id", str(nid))
    writes = []
    original = store.update_node
    monkeypatch.setattr(
        store, "update_node",
        lambda node: (writes.append((node.id, node.security)), original(node))[-1])
    calls = []
    monkeypatch.setattr(service, "apply_node", lambda node, *a, **k: calls.append(node.id))
    result = _refresh_with_feed(
        monkeypatch, store, settings, sid,
        [_live_reality(sid, security="tls", public_key=""),
         _live_reality(sid, address="9.9.9.9", name="fresh")])
    assert (nid, "tls") not in writes, "the downgrade was committed and then restored"
    assert result["ok"] is False and "downgrade" in result["error"]
    assert result["added"] == 1                    # the honest partial count, not a hiding zero
    assert calls == []
    assert store.get_node(nid).security == "reality"
    assert len(store.list_nodes_for_sub(sid)) == 2
    sub = store.get_subscription(sid)
    assert sub.last_status.startswith("error:") and "downgrade" in sub.last_status


def test_refresh_still_applies_a_same_rank_key_rotation(monkeypatch, settings):
    """The gate is one-directional. A reality feed rotating its public key (same rank, same
    identity) is the routine unattended change the panel exists to pick up."""
    store = _store(settings)
    sid = store.add_subscription(Subscription(id=None, name="x", url="https://93.184.216.34/s"))
    nid = store.add_node(_live_reality(sid))
    store.set_setting("active_node_id", str(nid))
    calls = []
    monkeypatch.setattr(service, "apply_node", lambda node, *a, **k: calls.append(node.id))
    result = _refresh_with_feed(monkeypatch, store, settings, sid,
                                [_live_reality(sid, public_key="ROTATED")])
    assert result["ok"] is True and calls == [nid]
    assert store.get_node(nid).public_key == "ROTATED"


# --- M3 (cycle 2): shutdown must bound the *process*, not just the drain ---
_WEDGED_SHUTDOWN_SCRIPT = """
import asyncio, threading, time
import pi_gw_panel.subs.scheduler as scheduler

scheduler.SHUTDOWN_TIMEOUT = 0.5
sched = scheduler.SubScheduler(None)
sched._pool = scheduler._new_pool()
sched._inflight[1] = sched._pool.submit(threading.Event().wait)   # never released
started = time.monotonic()
asyncio.run(sched.stop())
print("DRAINED %.2f" % (time.monotonic() - started), flush=True)
"""


def test_shutdown_is_bounded_for_the_whole_process_not_just_the_drain():
    """`_drain` returning promptly was never enough: a running future cannot be cancelled, and
    `ThreadPoolExecutor` joins its (non-daemon) workers from a `threading._register_atexit`
    hook, so the wedged refresh held the interpreter open regardless. Measured on the real
    process: it must exit, and the drain must respect SHUTDOWN_TIMEOUT."""
    import os
    import pathlib
    import subprocess
    import sys

    import pi_gw_panel

    root = pathlib.Path(pi_gw_panel.__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(root)}
    started = time.monotonic()
    proc = subprocess.run([sys.executable, "-c", _WEDGED_SHUTDOWN_SCRIPT], env=env, timeout=30,
                          capture_output=True, text=True)
    elapsed = time.monotonic() - started
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("DRAINED"), proc.stdout
    assert float(proc.stdout.split()[1]) < 5.0, proc.stdout      # drain honoured its deadline
    assert elapsed < 20.0, f"the process took {elapsed:.1f}s to exit over a wedged refresh"
