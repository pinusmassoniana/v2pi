import json
import logging
import shutil
import socket
import ssl
import subprocess
import threading
import time

import pytest

from pi_gw_panel.health import probe
from pi_gw_panel.health.probe import tcp_ping, real_request
from pi_gw_panel.models import Node


class _FakeConn:
    def close(self):
        pass


def test_tcp_ping_ok_via_injected_connect():
    # deadline (pre-lookup), dial start (post-lookup), answer: a literal address costs no
    # lookup time, so the 50 ms is the dial
    times = iter([0.0, 0.0, 0.05])                  # 50 ms
    ok, ms = tcp_ping("1.2.3.4", 443, connect=lambda addr, to: _FakeConn(),
                      clock=lambda: next(times))
    assert ok is True and ms == 50


def test_tcp_ping_failure_returns_false_none():
    def boom(addr, to):
        raise OSError("connection refused")
    ok, ms = tcp_ping("1.2.3.4", 443, connect=boom)
    assert ok is False and ms is None


class _FakeResp:
    def __init__(self, body: str, status: int = 200):
        self._body = body.encode()
        self.status = status

    def read(self, amt=None):
        return self._body if amt is None else self._body[:amt]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeOpener:
    def __init__(self, resp=None, exc=None):
        self._resp, self._exc = resp, exc

    def open(self, url, timeout=None):
        if self._exc:
            raise self._exc
        return self._resp


def test_real_request_parses_json_ip():
    times = iter([0.0, 0.123])
    ok, status, ms, ip = real_request(
        "http://127.0.0.1:10808", "https://api.ipify.org?format=json",
        opener_factory=lambda: _FakeOpener(_FakeResp('{"ip": "203.0.113.9"}')),
        clock=lambda: next(times))
    assert ok is True and status == 200 and ms == 123 and ip == "203.0.113.9"


def test_real_request_parses_httpbin_origin():
    ok, status, ms, ip = real_request(
        "http://127.0.0.1:10808", "https://httpbin.org/ip",
        opener_factory=lambda: _FakeOpener(_FakeResp('{"origin": "198.51.100.1"}')))
    assert ip == "198.51.100.1"


def test_real_request_parses_cloudflare_trace():
    ok, status, ms, ip = real_request(
        "http://127.0.0.1:10808", "https://1.1.1.1/cdn-cgi/trace",
        opener_factory=lambda: _FakeOpener(_FakeResp("fl=1\nip=198.51.100.7\nts=9")))
    assert ip == "198.51.100.7"


def test_real_request_parses_bare_ip():
    ok, status, ms, ip = real_request(
        "http://127.0.0.1:10808", "https://api.ipify.org",
        opener_factory=lambda: _FakeOpener(_FakeResp("203.0.113.42\n")))
    assert ip == "203.0.113.42"


def test_real_request_failure_returns_all_none():
    ok, status, ms, ip = real_request(
        "http://127.0.0.1:10808", "https://x",
        opener_factory=lambda: _FakeOpener(exc=OSError("proxy down")))
    assert ok is False and status is None and ms is None and ip is None


def test_real_request_non_2xx_is_not_ok():
    ok, status, ms, ip = real_request(
        "http://127.0.0.1:10808", "https://x",
        opener_factory=lambda: _FakeOpener(_FakeResp("", status=502)),
        clock=lambda: 0.0)
    assert ok is False and status == 502


# --- DNS rebinding: the checked address must be the dialled address ------------------------
#
# Validating the endpoint and then dialling the *hostname* leaves the check re-resolvable: a
# hostile feed only needs an answer that changes between the two. The probes run on a timer as
# root and publish the outcome through the node-health API, so that is a recurring internal
# port scan. Every probe must dial the IP the check returned.

def _rebinding_dns(monkeypatch, first="1.2.3.7", later="127.0.0.1"):
    """A resolver that answers public exactly once and private on every lookup after it."""
    lookups = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        lookups.append(host)
        ip = first if len(lookups) == 1 else later
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return lookups


def _mixed_dns(monkeypatch, public="1.2.3.7", private="10.0.0.5"):
    """One answer carrying a public *and* a private record — picking the public one and
    dialling would still hand the feed a scan through the other."""
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                for ip in (public, private)]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def test_tcp_ping_dials_the_pinned_ip_not_the_rebound_hostname(monkeypatch):
    _rebinding_dns(monkeypatch)
    dialled = []
    ok, _ms = tcp_ping("rebind.example", 443,
                       connect=lambda addr, to: dialled.append(addr) or _FakeConn())
    assert ok is True
    assert dialled == [("1.2.3.7", 443)]      # never the hostname (the OS would re-resolve)


def test_http_ping_dials_the_pinned_ip_and_keeps_the_hostname_as_sni(monkeypatch):
    _rebinding_dns(monkeypatch)
    dialled, sni_seen = [], []

    class _Ctx:
        check_hostname = True
        verify_mode = None

        def wrap_socket(self, sock, server_hostname=None):
            sni_seen.append(server_hostname)
            return sock

    monkeypatch.setattr(socket, "create_connection",
                        lambda addr, to: dialled.append(addr) or _FakeConn())
    monkeypatch.setattr(ssl, "create_default_context", lambda: _Ctx())
    # no explicit sni on the node — the hostname still has to reach the wire as SNI, or a
    # legitimately CNAME'd endpoint would be handed a bare IP and fail its handshake
    ok, _ms = probe.http_ping("cdn.example", 443, "")
    assert ok is True
    assert dialled == [("1.2.3.7", 443)]
    assert sni_seen == ["cdn.example"]


def test_real_through_node_pins_the_ip_into_the_throwaway_xray_config(monkeypatch):
    _rebinding_dns(monkeypatch)
    monkeypatch.setattr(probe, "real_request",
                        lambda proxy, url, timeout=5.0: (True, 200, 5, "9.9.9.9"))
    spawned = []

    class _Proc:
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout=None): pass

    def fake_spawn(path):
        with open(path) as fh:
            spawned.append(json.load(fh))
        return _Proc()

    node = Node(id=1, name="n", address="rebind.example", port=443, uuid="u",
                security="reality", public_key="PK", short_id="ab")
    ok, _ms, _egress, _egress6 = probe.real_through_node(
        node, "xray", "https://probe", spawn=fake_spawn, wait_ready=lambda p: None)
    assert ok is True
    vnext = spawned[0]["outbounds"][0]["settings"]["vnext"][0]
    # xray must dial the validated IP; re-resolving the hostname inside the throwaway instance
    # is exactly the bypass the check was meant to close
    assert vnext["address"] == "1.2.3.7"
    reality = spawned[0]["outbounds"][0]["streamSettings"]["realitySettings"]
    assert reality["serverName"] == "rebind.example"


@pytest.mark.parametrize("port", [443, 8443])
def test_probes_refuse_a_mixed_public_and_private_dns_answer(monkeypatch, port):
    _mixed_dns(monkeypatch)
    dialled = []
    assert tcp_ping("mixed.example", port,
                    connect=lambda addr, to: dialled.append(addr) or _FakeConn()) == (False, None)
    node = Node(id=1, name="n", address="mixed.example", port=port, uuid="u")
    assert probe.real_through_node(
        node, "xray", "https://probe",
        spawn=lambda _p: (_ for _ in ()).throw(AssertionError("must not spawn xray")),
    ) == (False, None, None, None)
    assert dialled == []


# --- the probe deadline is wall-clock, not urllib's per-socket idle timer ------------------
#
# A slow-drip server resets an idle timeout forever. `real_request` runs on the single liveness
# worker, so an unbounded read freezes the xray watchdog and auto-failover with it.

def _loopback_http_server(handle) -> tuple[socket.socket, int]:
    """A one-shot HTTP server on loopback. `handle(conn)` writes the response. No outbound
    traffic: the probe reaches it as its own local proxy."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        try:
            conn.recv(65536)
            handle(conn)
        except OSError:
            pass
        finally:
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv, srv.getsockname()[1]


def _dribble(conn, prefix: bytes, seconds: float = 6.0):
    conn.sendall(prefix)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        conn.sendall(b"x")
        time.sleep(0.05)


_UNFINISHED = object()


def _bounded_call(fn, limit: float):
    """Run `fn` on a daemon thread and give up after `limit` — a probe that is not actually
    bounded must fail this test, not hang the suite."""
    box = {}
    thread = threading.Thread(target=lambda: box.setdefault("r", fn()), daemon=True)
    started = time.monotonic()
    thread.start()
    thread.join(limit)
    return box.get("r", _UNFINISHED), time.monotonic() - started


@pytest.fixture
def _no_proxy_bypass(monkeypatch):
    # the probe URL is never resolved: it is handed to the (loopback) proxy verbatim
    import urllib.request
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)


def test_real_request_returns_within_the_deadline_on_a_drip_fed_body(_no_proxy_bypass):
    srv, port = _loopback_http_server(
        lambda conn: _dribble(conn, b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n"))
    try:
        result, elapsed = _bounded_call(
            lambda: real_request(f"http://127.0.0.1:{port}", "http://probe.invalid/ip",
                                 timeout=1.0), 5.0)
    finally:
        srv.close()
    assert result is not _UNFINISHED, "real_request never returned — the deadline is not hard"
    assert elapsed < 3.0
    assert result == (False, None, None, None)


def test_real_request_returns_within_the_deadline_on_drip_fed_headers(_no_proxy_bypass):
    # the drip starts before the response is even complete, so urllib is still inside open()
    srv, port = _loopback_http_server(lambda conn: _dribble(conn, b"HTTP/1.1 200 OK\r\nX-Pad: "))
    try:
        result, elapsed = _bounded_call(
            lambda: real_request(f"http://127.0.0.1:{port}", "http://probe.invalid/ip",
                                 timeout=1.0), 5.0)
    finally:
        srv.close()
    assert result is not _UNFINISHED, "real_request never returned — the deadline is not hard"
    assert elapsed < 3.0
    assert result == (False, None, None, None)


def test_real_request_still_reads_a_prompt_response_through_the_real_opener(_no_proxy_bypass):
    body = b'{"ip": "203.0.113.5"}'

    def respond(conn):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))

    srv, port = _loopback_http_server(respond)
    try:
        result, _elapsed = _bounded_call(
            lambda: real_request(f"http://127.0.0.1:{port}", "http://probe.invalid/ip",
                                 timeout=5.0), 10.0)
    finally:
        srv.close()
    ok, status, _ms, egress = result
    assert (ok, status, egress) == (True, 200, "203.0.113.5")


# --- …and the deadline has to cover TLS, not only plaintext -------------------------------
#
# The real probe URL is https. `ssl.SSLContext.wrap_socket` *detaches* the socket it wraps —
# the object http.client created is left at fd -1 while the new SSLSocket owns the descriptor —
# so a guard armed only with that raw socket holds a dead handle for the handshake and for
# every read after it. The stall then outlives the deadline exactly as it did before the guard.


def _connect_established(handle):
    """Turn `handle` into a one-shot HTTP proxy: answer CONNECT with 200, then let the handler
    own the tunnel. Nothing leaves the host — the "tunnel" is the same loopback socket."""
    def serve(conn):
        conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        handle(conn)
    return serve


@pytest.fixture(scope="module")
def _loopback_tls_cert(tmp_path_factory):
    """A throwaway self-signed cert for the loopback TLS server (no network, no fixture file
    that can expire in the repo)."""
    openssl = shutil.which("openssl")
    if not openssl:                                    # pragma: no cover - env dependent
        pytest.skip("openssl is required to mint a loopback TLS certificate")
    directory = tmp_path_factory.mktemp("probe-tls")
    cert, key = directory / "cert.pem", directory / "key.pem"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=probe.invalid", "-keyout", str(key), "-out", str(cert)],
                   check=True, capture_output=True)
    return str(cert), str(key)


@pytest.fixture
def _trust_the_loopback_cert(monkeypatch):
    """Relax *only* certificate verification: the guarded https handler under test — and the
    connection class it installs — stay exactly the ones the probe uses in production."""
    class _Unverified(probe._GuardedHTTPSHandler):
        def __init__(self, guard):
            super().__init__(guard)
            self._context.check_hostname = False
            self._context.verify_mode = ssl.CERT_NONE

    monkeypatch.setattr(probe, "_GuardedHTTPSHandler", _Unverified)


def test_a_stalled_tls_handshake_is_cut_at_the_deadline_not_a_timeout_later(_no_proxy_bypass):
    """A stalled handshake is not infinite — CPython bounds one ``do_handshake`` by the socket
    timeout — but that bound starts when the handshake does, so everything spent before it is
    free. Here the proxy burns 80% of the budget answering CONNECT and only then stalls: a
    guard holding the detached raw socket cuts nothing, and the probe runs on to roughly two
    deadlines. That doubling is the liveness worker (and the failover it drives) sitting idle."""
    def slow_connect_then_stall(conn):
        time.sleep(1.2)                       # 80% of the 1.5 s budget, before TLS even starts
        conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        conn.recv(65536)                      # the ClientHello
        # a handshake record header promising 16 KiB, then one byte at a time — enough to keep
        # OpenSSL waiting for the rest of a ServerHello that never arrives
        _dribble(conn, b"\x16\x03\x03\x40\x00")

    srv, port = _loopback_http_server(slow_connect_then_stall)
    try:
        result, elapsed = _bounded_call(
            lambda: real_request(f"http://127.0.0.1:{port}", "https://probe.invalid/ip",
                                 timeout=1.5), 6.0)
    finally:
        srv.close()
    assert result is not _UNFINISHED, "real_request never returned — the TLS handshake is unbounded"
    assert elapsed < 2.0, f"the 1.5 s deadline is not wall-clock: returned after {elapsed:.2f}s"
    assert result == (False, None, None, None)


def test_real_request_returns_within_the_deadline_on_a_drip_fed_tls_body(
        _no_proxy_bypass, _loopback_tls_cert, _trust_the_loopback_cert):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*_loopback_tls_cert)

    def dribble_after_handshake(conn):
        tls = context.wrap_socket(conn, server_side=True)
        try:
            tls.recv(65536)                   # the GET, now inside TLS
            _dribble(tls, b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
        finally:
            tls.close()

    srv, port = _loopback_http_server(_connect_established(dribble_after_handshake))
    try:
        result, elapsed = _bounded_call(
            lambda: real_request(f"http://127.0.0.1:{port}", "https://probe.invalid/ip",
                                 timeout=1.0), 5.0)
    finally:
        srv.close()
    assert result is not _UNFINISHED, "real_request never returned — the TLS read is unbounded"
    assert elapsed < 3.0
    assert result == (False, None, None, None)


def test_real_request_still_reads_a_prompt_tls_response(
        _no_proxy_bypass, _loopback_tls_cert, _trust_the_loopback_cert):
    """The other half of the contract: bounding the handshake must not break a healthy one."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*_loopback_tls_cert)
    body = b'{"ip": "203.0.113.6"}'

    def respond(conn):
        tls = context.wrap_socket(conn, server_side=True)
        try:
            tls.recv(65536)
            tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
        finally:
            tls.close()

    srv, port = _loopback_http_server(_connect_established(respond))
    try:
        result, _elapsed = _bounded_call(
            lambda: real_request(f"http://127.0.0.1:{port}", "https://probe.invalid/ip",
                                 timeout=5.0), 10.0)
    finally:
        srv.close()
    ok, status, _ms, egress = result
    assert (ok, status, egress) == (True, 200, "203.0.113.6")


# --- one dead address out of a healthy answer is not a dead node --------------------------
#
# Pinning the *first* validated address turns a multi-A/AAAA endpoint unhealthy the moment that
# one address is down, and failover reads exactly these two probes — it would migrate off a
# node that still answers on its other addresses.


def _multi_dns(monkeypatch, *ips):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 (ip, port)) for ip in ips]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def test_resolve_endpoints_returns_every_validated_address_first_one_pinned(monkeypatch):
    _multi_dns(monkeypatch, "1.2.3.7", "1.2.3.8")
    assert probe.resolve_endpoints("multi.example", 443) == ["1.2.3.7", "1.2.3.8"]
    # callers that can only dial one (the throwaway-xray probe, the API) keep the first
    assert probe.resolve_endpoint("multi.example", 443) == "1.2.3.7"


def test_tcp_ping_falls_over_to_the_next_address_when_the_first_is_dead(monkeypatch):
    _multi_dns(monkeypatch, "1.2.3.7", "1.2.3.8")
    dialled = []

    def connect(addr, to):
        dialled.append(addr)
        if addr[0] == "1.2.3.7":
            raise OSError("connection refused")
        return _FakeConn()

    ok, ms = tcp_ping("multi.example", 443, connect=connect)
    assert ok is True and ms is not None
    assert dialled == [("1.2.3.7", 443), ("1.2.3.8", 443)]


def test_http_ping_falls_over_to_the_next_address_when_the_first_is_dead(monkeypatch):
    _multi_dns(monkeypatch, "1.2.3.7", "1.2.3.8")
    dialled = []

    def connect(addr, to):
        dialled.append(addr)
        if addr[0] == "1.2.3.7":
            raise ssl.SSLError("handshake failed")
        return _FakeConn()

    ok, ms = probe.http_ping("multi.example", 443, "sni.example", connect=connect)
    assert ok is True and ms is not None
    assert dialled == [("1.2.3.7", 443), ("1.2.3.8", 443)]


def test_tcp_ping_spends_one_timeout_across_every_address(monkeypatch):
    """The budget belongs to the probe, not to each address: the sweep visits every node in
    turn, so N addresses must not cost N timeouts."""
    _multi_dns(monkeypatch, "1.2.3.7", "1.2.3.8", "1.2.3.9")
    attempts = []
    # deadline, dial start (the stubbed lookup costs nothing), then 0.6 s burned per failed
    # attempt
    clock = iter([0.0, 0.0, 0.6, 1.2])

    def connect(addr, to):
        attempts.append((addr[0], to))
        raise OSError("timed out")

    assert tcp_ping("multi.example", 443, timeout=1.0, connect=connect,
                    clock=lambda: next(clock)) == (False, None)
    assert [ip for ip, _to in attempts] == ["1.2.3.7", "1.2.3.8"]   # budget gone before the third
    assert attempts[0][1] == 1.0
    assert attempts[1][1] == pytest.approx(0.4)


def test_probes_never_walk_from_a_public_address_into_a_private_one(monkeypatch):
    """Failing over between the addresses of one answer must not become the way in: the whole
    answer is validated, so a public-then-private answer is still refused outright."""
    _mixed_dns(monkeypatch, public="1.2.3.7", private="10.0.0.5")
    dialled = []

    def connect(addr, to):
        dialled.append(addr)
        raise OSError("refused")

    assert tcp_ping("mixed.example", 443, connect=connect) == (False, None)
    assert probe.http_ping("mixed.example", 443, "sni", connect=connect) == (False, None)
    assert probe.resolve_endpoints("mixed.example", 443) == []
    assert dialled == []


# --- the advertised timeout covers the lookup and the dial together -----------------------
#
# Resolving under a timeout of its own and *then* dialling under another is two budgets wearing
# one name: a DNS answer that lands just before its deadline is followed by a full connect
# deadline, so the bound the sweep (and the failover reading it) was promised is quietly worth
# twice as much. The deadline has to be armed before the lookup and carried through both phases.


def _lookup_costing(monkeypatch, seconds: float, *ips: str):
    """Replace the lookup with one that spends `seconds` of a fake clock and answers `ips`.
    Returns that clock, so the probe under test and the assertions share one timeline."""
    now = {"t": 0.0}

    def fake_resolve(address, port=443, timeout=5.0, allow_private=False):
        now["t"] += seconds
        return list(ips)

    monkeypatch.setattr(probe, "resolve_endpoints", fake_resolve)
    return lambda: now["t"]


def test_tcp_ping_takes_the_lookup_out_of_the_dial_budget(monkeypatch):
    clock = _lookup_costing(monkeypatch, 0.4, "1.2.3.7")
    attempts = []

    ok, _ms = tcp_ping("slow-dns.example", 443, timeout=1.0, clock=clock,
                       connect=lambda addr, to: attempts.append(to) or _FakeConn())
    assert ok is True
    assert attempts == [pytest.approx(0.6)]


def test_http_ping_takes_the_lookup_out_of_the_handshake_budget(monkeypatch):
    clock = _lookup_costing(monkeypatch, 0.4, "1.2.3.7")
    attempts = []

    ok, _ms = probe.http_ping("slow-dns.example", 443, "sni.example", timeout=1.0, clock=clock,
                              connect=lambda addr, to: attempts.append(to) or _FakeConn())
    assert ok is True
    assert attempts == [pytest.approx(0.6)]


def test_a_lookup_that_spends_the_budget_leaves_nothing_to_dial_with(monkeypatch):
    """Not a shorter dial — none at all. The sweep visits every node in turn, so a node whose
    resolver is the slow part must not still be handed a full connect budget on top of it."""
    clock = _lookup_costing(monkeypatch, 1.0, "1.2.3.7", "1.2.3.8")
    dialled = []

    def connect(addr, to):
        dialled.append(addr)
        return _FakeConn()

    assert tcp_ping("slow-dns.example", 443, timeout=1.0, clock=clock,
                    connect=connect) == (False, None)
    assert probe.http_ping("slow-dns.example", 443, "sni", timeout=1.0, clock=clock,
                           connect=connect) == (False, None)
    assert dialled == []


def test_the_probe_bound_is_the_wall_clock_for_both_phases(monkeypatch):
    """The same claim against a real clock, since a fake one can only prove the arithmetic: a
    lookup that spends the whole budget and a dial that would spend it again. Nothing leaves the
    host — both phases are local stubs that sleep."""
    budget = 0.2

    def slow_lookup(address, port=443, timeout=5.0, allow_private=False):
        time.sleep(budget)
        return ["1.2.3.7"]

    def slow_dial(addr, to):
        time.sleep(budget)
        return _FakeConn()

    monkeypatch.setattr(probe, "resolve_endpoints", slow_lookup)
    started = time.monotonic()
    ok, _ms = tcp_ping("slow-dns.example", 443, timeout=budget, connect=slow_dial)
    elapsed = time.monotonic() - started
    assert elapsed < budget * 1.6, \
        f"the {budget}s bound is per phase, not end to end: returned after {elapsed:.3f}s"
    assert ok is False, "the budget was gone; the dial happened anyway"


# --- a guard that cannot take a duplicate is not a weaker guard, it is no guard ------------


def test_a_socket_that_cannot_be_duplicated_fails_the_probe(_no_proxy_bypass, monkeypatch):
    """`dup` fails under FD pressure — precisely when a loaded box needs its deadline. Arming
    the raw socket instead reads like a graceful fallback, but the TLS wrap detaches exactly
    that socket, so the hard bound quietly reverts to the old ~two-deadline one with nothing
    reporting the downgrade. Fail the probe instead, and close the socket it cannot guard."""
    create = socket.create_connection
    closed = []

    class _Undupable:
        """A real connected socket that refuses to duplicate (EMFILE)."""

        def __init__(self, sock):
            self._sock = sock

        def dup(self):
            raise OSError(24, "Too many open files")

        def close(self):
            closed.append(True)
            self._sock.close()

        def __getattr__(self, name):
            return getattr(self._sock, name)

    monkeypatch.setattr(socket, "create_connection",
                        lambda *a, **kw: _Undupable(create(*a, **kw)))
    body = b'{"ip": "203.0.113.8"}'
    srv, port = _loopback_http_server(
        lambda conn: conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s"
                                  % (len(body), body)))
    try:
        result, _elapsed = _bounded_call(
            lambda: real_request(f"http://127.0.0.1:{port}", "http://probe.invalid/ip",
                                 timeout=5.0), 10.0)
    finally:
        srv.close()
    assert result == (False, None, None, None), \
        "the probe ran on with a deadline it could not enforce"
    assert closed == [True], "the socket that could not be guarded was left open"


# --- the throwaway-xray probe spends ONE deadline, not one per phase -----------------------
#
# `real_through_node` resolves the endpoint, waits for a slot on the process-wide probe gate,
# waits for the throwaway instance to come up, makes one or two requests and tears it down.
# Every phase used to carry a budget of its own — its own resolution timeout, an *indefinite*
# wait on the gate, its own readiness wait, a full `timeout` per request — so an 8 s probe was
# worth several times that to whoever was slow, and unbounded whenever the gate was busy. The
# per-phase tests above cannot see that: each phase behaves, only the total is wrong. This is
# the composed one.


class _Clock:
    """A clock the test moves by hand: every phase spends from it, so the whole probe runs on
    one timeline with nothing left to real wall-clock scheduling."""

    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def spend(self, seconds: float) -> None:
        self.t += seconds


class _RecordingProc:
    def __init__(self, seen):
        self._seen = seen

    def poll(self):                                   # like Popen: None while it is running
        return None

    def terminate(self):
        self._seen["terminated"] = True

    def wait(self, timeout=None):
        self._seen["teardown"] = timeout

    def kill(self):                                   # pragma: no cover - the double exits
        self._seen["killed"] = True


def test_the_throwaway_probe_spends_one_deadline_across_every_phase(monkeypatch):
    clock = _Clock()
    seen: dict = {}

    def fake_resolve(address, port=443, timeout=5.0, allow_private=False):
        seen["resolve"] = timeout
        clock.spend(2.0)
        return "1.2.3.7"

    class _BusyGate:
        def acquire(self, timeout=None):
            seen["gate"] = timeout
            clock.spend(3.0)                  # every slot taken; one frees up three seconds in
            return True

        def release(self):
            seen["released"] = True

    def wait_ready(port):
        clock.spend(1.0)

    def fake_real_request(proxy, url, timeout=5.0):
        seen.setdefault("requests", []).append(timeout)
        clock.spend(timeout / 2)              # each request answers halfway into its budget
        return True, 200, 10, "9.9.9.9" if url.endswith("v4") else "2606::5"

    monkeypatch.setattr(probe, "resolve_endpoint", fake_resolve)
    monkeypatch.setattr(probe, "real_request", fake_real_request)
    node = Node(id=1, name="n", address="node.example", port=443, uuid="u")
    ok, _ms, egress, egress6 = probe.real_through_node(
        node, "xray", "https://v4", timeout=8.0, spawn=lambda _p: _RecordingProc(seen),
        wait_ready=wait_ready, probe_url6="https://v6", clock=clock, sem=_BusyGate())

    assert (ok, egress, egress6) == (True, "9.9.9.9", "2606::5")
    # every phase was handed what was LEFT of the 8 s, never a fresh copy of it
    assert seen["resolve"] == pytest.approx(8.0)
    assert seen["gate"] == pytest.approx(6.0)                       # 8 − 2 s of resolution
    assert seen["requests"] == [pytest.approx(2.0), pytest.approx(1.0)]   # …− 3 s gate − 1 s wait
    assert seen["teardown"] == pytest.approx(0.5)                   # even the teardown is inside
    assert seen["released"] is True
    assert clock() <= 8.0, f"the 8 s bound is per phase, not end to end: spent {clock():.1f}s"


def test_a_contended_probe_gate_fails_the_probe_instead_of_waiting_it_out(monkeypatch):
    """The gate is process-wide and holds three slots. Waiting on it without a bound is how a
    probe that advertises 8 s pins its caller for as long as three other probes take — and the
    caller is the liveness worker, with the xray watchdog and auto-failover behind it. A probe
    that never got a slot has to fail, not queue."""
    clock = _Clock()
    asked = []

    class _Contended:
        def acquire(self, timeout=None):
            asked.append(timeout)
            clock.spend(timeout)              # the whole budget goes on the wait, and no slot
            return False

        def release(self):                    # pragma: no cover - nothing was acquired
            raise AssertionError("released a slot it never took")

    monkeypatch.setattr(probe, "resolve_endpoint", lambda *_a, **_k: "1.2.3.7")
    node = Node(id=1, name="n", address="node.example", port=443, uuid="u")
    assert probe.real_through_node(
        node, "xray", "https://v4", timeout=4.0, clock=clock, sem=_Contended(),
        spawn=lambda _p: (_ for _ in ()).throw(AssertionError("must not spawn xray")),
        wait_ready=lambda _p: None) == (False, None, None, None)
    assert asked == [pytest.approx(4.0)], "the gate was waited on without a bound"
    assert clock() <= 4.0


def test_a_resolution_that_spends_the_budget_never_reaches_the_gate(monkeypatch):
    """Not a shorter wait — none at all. A slow resolver must not still buy a full slot wait,
    a spawn and a request on top of the budget it already spent."""
    clock = _Clock()

    def slow_resolve(address, port=443, timeout=5.0, allow_private=False):
        clock.spend(2.5)
        return "1.2.3.7"

    class _Untouched:
        def acquire(self, timeout=None):      # pragma: no cover - the point is it isn't reached
            raise AssertionError("the budget was gone; the gate was waited on anyway")

    monkeypatch.setattr(probe, "resolve_endpoint", slow_resolve)
    node = Node(id=1, name="n", address="node.example", port=443, uuid="u")
    assert probe.real_through_node(
        node, "xray", "https://v4", timeout=2.0, clock=clock, sem=_Untouched(),
        spawn=lambda _p: (_ for _ in ()).throw(AssertionError("must not spawn xray")),
        wait_ready=lambda _p: None) == (False, None, None, None)


# --- an operator's own node on the LAN is probed; a feed's "LAN node" still never is -------
#
# The guard above exists so a subscription feed cannot aim the probes at internal services, and
# it was applied to every node whatever its origin — which also refused the one internal address
# an operator is entitled to point the panel at: their own box on the same LAN. That node then
# reads as permanently dead (both probes return (False, None) with nothing dialled) and
# auto-failover, which only promotes a node something has marked alive, can never pick it.
# `allow_private` is provenance, and it relaxes exactly the private/link-local half of the check.


@pytest.mark.parametrize("address", ["192.168.1.10", "10.0.0.5", "172.16.4.4", "169.254.1.1",
                                     "fd00::5", "fe80::1"])
def test_a_private_endpoint_is_dialled_only_when_the_node_is_operator_added(address):
    dialled = []

    def connect(addr, to):
        dialled.append(addr[0])
        return _FakeConn()

    # the security half, and the one that must never regress: without the flag — a feed-imported
    # node, or any caller that simply forgot it — the address is refused and nothing is dialled
    assert tcp_ping(address, 443, connect=connect) == (False, None)
    assert probe.http_ping(address, 443, "sni", connect=connect) == (False, None)
    assert probe.resolve_endpoints(address, 443) == []
    assert dialled == []
    # …and with it, the operator's own node is an ordinary probeable endpoint
    assert tcp_ping(address, 443, connect=connect, allow_private=True)[0] is True
    assert probe.http_ping(address, 443, "sni", connect=connect, allow_private=True)[0] is True
    assert dialled == [address, address]


@pytest.mark.parametrize("address", ["127.0.0.1", "::1", "localhost", "224.0.0.1", "ff02::1",
                                     "0.0.0.0", "::", "240.0.0.1", ""])
def test_allow_private_still_refuses_what_no_node_may_be_probed_at(address):
    """The relaxed check drops the private/link-local half and nothing else: loopback would aim
    the probe at the panel host itself (its admin port, its local proxy inbound), and multicast,
    the unspecified address and the reserved ranges are not node endpoints at all."""
    def connect(addr, to):
        raise AssertionError(f"must not dial {addr}")

    assert tcp_ping(address, 443, connect=connect, allow_private=True) == (False, None)
    assert probe.http_ping(address, 443, "sni", connect=connect,
                           allow_private=True) == (False, None)
    assert probe.resolve_endpoints(address, 443, allow_private=True) == []


def test_a_relaxed_lookup_keeps_pinning_and_still_validates_the_whole_answer(monkeypatch):
    """Relaxing *which* addresses are allowed must not relax *how* they are handled: the answer
    is still resolved once and dialled by IP (a rebinding answer cannot slip loopback in behind
    the check), and a single forbidden record still refuses the whole answer."""
    _rebinding_dns(monkeypatch, first="192.168.1.10", later="127.0.0.1")
    dialled = []
    ok, _ms = tcp_ping("lan.example", 443, allow_private=True,
                       connect=lambda addr, to: dialled.append(addr) or _FakeConn())
    assert ok is True and dialled == [("192.168.1.10", 443)]

    _mixed_dns(monkeypatch, public="192.168.1.10", private="127.0.0.1")
    assert probe.resolve_endpoints("lan.example", 443, allow_private=True) == []


@pytest.mark.parametrize("deadline, spent", [(0.4, 0.4), (30.0, 3.0)])
def test_the_readiness_wait_stops_at_whichever_bound_runs_out_first(monkeypatch, deadline, spent):
    """The readiness wait has a cap of its own (`_READY_WAIT`, 3 s) — fine while the probe still
    has that much budget, a lie when it doesn't. Whichever bound runs out first has to win, and
    no single attempt may dial for longer than what is left."""
    assert probe._READY_WAIT == 3.0
    clock = _Clock()
    attempts = []

    def refuse(addr, timeout):
        attempts.append(timeout)
        clock.spend(0.05)
        raise OSError("connection refused")

    monkeypatch.setattr(socket, "create_connection", refuse)
    probe._wait_ready(9, deadline=deadline, clock=clock, sleep=clock.spend)
    assert clock() == pytest.approx(spent), "the readiness wait outlived its bound"
    assert 0 < min(attempts) and max(attempts) <= 0.2


# --- …and the teardown is inside that one deadline, kill included ---------------------------
#
# The bounded wait after `terminate()` was followed by a bare `proc.wait()` after `kill()`. An
# xray stuck in an uninterruptible syscall is not reaped by SIGKILL either, so the probe that
# advertises ONE deadline could sit in its own `finally` forever — freezing the liveness worker
# and, behind it, the xray watchdog and auto-failover. Bounding that second wait separately was
# only half of it: an allowance of its own is still time the deadline does not have, so both
# waits now spend what is left of the one budget and nothing on top of it.


class _UnkillableProc:
    """Ignores both signals, and refuses any wait that has no bound."""

    def __init__(self):
        self.waits = []
        self.terminated = False
        self.killed = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if timeout is None:
            raise AssertionError(
                "wait() with no timeout — the probe is blocked in its own teardown, holding "
                "the liveness worker")
        raise subprocess.TimeoutExpired("xray", timeout)

    @property
    def pid(self):
        return 4321


def test_a_throwaway_xray_that_ignores_sigkill_never_blocks_the_teardown(monkeypatch, caplog):
    clock = _Clock()
    proc = _UnkillableProc()
    monkeypatch.setattr(probe, "resolve_endpoint", lambda *_a, **_k: "1.2.3.7")
    monkeypatch.setattr(probe, "real_request",
                        lambda proxy, url, timeout=5.0: (True, 200, 7, "9.9.9.9"))

    class _FreeGate:
        def acquire(self, timeout=None):
            return True

        def release(self):
            pass

    node = Node(id=1, name="n", address="node.example", port=443, uuid="u")
    with caplog.at_level(logging.ERROR, logger="pi_gw_panel"):
        ok, _ms, egress, _egress6 = probe.real_through_node(
            node, "xray", "https://v4", timeout=8.0, spawn=lambda _p: proc,
            wait_ready=lambda _p: None, clock=clock, sem=_FreeGate())

    assert proc.terminated is True and proc.killed is True
    assert proc.waits and all(t is not None for t in proc.waits), "a wait ran unbounded"
    assert len(proc.waits) == 2, "the post-kill reap was skipped, not bounded"
    assert proc.waits[1] <= probe._REAP_WAIT
    # the failed stop is reported, not swallowed…
    assert any("leaked an xray" in r.getMessage() for r in caplog.records), \
        "a leaked xray was torn down in silence"
    # …and the measurement that did happen is still what the probe returns: a node that answered
    # is not unhealthy because our own teardown failed.
    assert (ok, egress) == (True, "9.9.9.9")


def test_a_teardown_at_the_deadline_gets_no_allowance_past_it(monkeypatch, caplog):
    """Both teardown waits come out of what is LEFT of the one deadline, and at the deadline
    that is zero — for the reap as much as for the SIGTERM grace.

    A `_REAP_WAIT` granted *in addition to* an exhausted budget is not a bound on the probe, it
    is a second of overrun per stuck probe charged to the liveness worker that drives failover.
    Zero is not the unbounded wait by another name: `wait(0)` returns or raises at once, and a
    child that is still there is still reported as a survivor — which is what the caller needs
    to know, and all this last wait was ever for."""
    clock = _Clock()
    proc = _UnkillableProc()
    monkeypatch.setattr(probe, "resolve_endpoint", lambda *_a, **_k: "1.2.3.7")

    def spend_it_all(proxy, url, timeout=5.0):
        clock.spend(timeout)                       # the request uses every second of the budget
        return False, None, None, None

    monkeypatch.setattr(probe, "real_request", spend_it_all)

    class _FreeGate:
        def acquire(self, timeout=None):
            return True

        def release(self):
            pass

    node = Node(id=1, name="n", address="node.example", port=443, uuid="u")
    with caplog.at_level(logging.ERROR, logger="pi_gw_panel"):
        assert probe.real_through_node(
            node, "xray", "https://v4", timeout=4.0, spawn=lambda _p: proc,
            wait_ready=lambda _p: None, clock=clock, sem=_FreeGate()) == (False, None, None, None)
    assert proc.waits == [0.0, 0.0], "the teardown bought time the deadline no longer had"
    assert proc.killed is True, "the reap was skipped rather than bounded at zero"
    assert clock() <= 4.0, f"the 4 s bound was overrun by the teardown: spent {clock():.1f}s"
    # …and a survivor is still never reported as stopped, however little budget was left to try.
    assert any("leaked an xray" in r.getMessage() for r in caplog.records), \
        "a leaked xray was torn down in silence"
