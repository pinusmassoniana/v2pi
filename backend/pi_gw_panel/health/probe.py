"""Network probes for the health subsystem (stdlib socket + urllib).

Both functions are blocking I/O, designed to be offloaded to a thread by the
monitor and fully stubbed in tests via the injected `connect` / `opener_factory`
/ `clock` seams — no real network is touched on the dev host. Real probing is
exercised on the Pi (Plan 8)."""
import http.client
import ipaddress
import json
import logging
import os
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.request

log = logging.getLogger("pi_gw_panel")

_PROBE_BODY_CAP = 64_000   # NS1: IP-echo responses are tiny; cap so a huge body can't OOM
# NR4: bound how many throwaway-xray probes can run at once (per-node "T" / real-test-all)
_PROBE_SEM = threading.BoundedSemaphore(3)


def _fetcher():
    """The subscription fetcher, imported lazily (it must not import the health package).

    Two of its internals are the mechanism of record for boundaries this module shares:
    ``_resolve_public_all`` (resolve once, refuse a mixed public/private answer, return the
    addresses to pin) and ``_DeadlineGuard`` (a hard wall-clock stop that shuts the live socket
    down). The probes reuse both rather than growing a second copy that can drift.
    """
    from pi_gw_panel.subs import fetcher
    return fetcher


def resolve_endpoints(address: str | None, port: int = 443,
                      timeout: float = 5.0) -> list[str]:
    """Resolve a feed-supplied node endpoint once and return **every IP it is allowed to dial**,
    in resolver order — empty when the endpoint is not a public address.

    ``address``/``port``/``sni`` are parsed straight out of a remote subscription feed and are
    then probed on a timer as root, with the outcome (reachable? how fast?) reported back
    through the node-health API — an internal port scanner driven by whoever writes the feed.
    Answering "is this allowed?" and then dialling the *hostname* does not close that: the dial
    re-resolves, and a feed only needs a DNS answer that changes between the two (rebinding) to
    get the scan it was refused. So the check hands back the addresses it validated and every
    caller dials one of those, keeping the original hostname only as TLS SNI.

    The whole answer is validated, not just the address that gets dialled: a mixed
    public/private answer is refused outright, so walking the list cannot reach an internal
    address the first entry was hiding.
    """
    if not address:
        return []
    try:
        return _fetcher()._resolve_public_all(address, port, time.monotonic() + timeout)
    except (ValueError, TimeoutError, OSError):
        # debug, not warning: a pool full of unreachable nodes would otherwise log on every sweep
        log.debug("probe refused: %r is not a public address", address)
        return []


def resolve_endpoint(address: str | None, port: int = 443,
                     timeout: float = 5.0) -> str | None:
    """The single IP to dial for callers that can only use one (see `resolve_endpoints`), or
    None if the endpoint is not a public address."""
    addresses = resolve_endpoints(address, port, timeout)
    return addresses[0] if addresses else None


def address_allowed(address: str | None) -> bool:
    """Whether it is safe to dial this node endpoint at all.

    Kept for callers that only need the predicate. Anything that goes on to *dial* must use
    `resolve_endpoint` and dial the returned IP instead — see the rebinding note there.
    """
    return resolve_endpoint(address) is not None


def tcp_ping(address: str, port: int, timeout: float = 3.0,
             connect=socket.create_connection, clock=time.monotonic) -> tuple[bool, int | None]:
    """TCP-connect liveness probe. Returns ``(ok, latency_ms)``; ``latency_ms`` is
    None when the connection fails (refused / timeout / unreachable).

    An endpoint with several A/AAAA records is tried in resolver order until one answers: an
    address that happens to be down must not report the whole node dead, because failover reads
    this and would migrate off a node that still works. ``timeout`` stays the budget for the
    *probe*, not per address — the sweep visits every node in turn, so multiplying the timeout
    by the size of a DNS answer is how one endpoint stalls the whole sweep.

    The lookup spends that same budget. A deadline armed *before* resolving and carried into the
    dials is what makes ``timeout`` the end-to-end wall clock: give resolution a timeout of its
    own and a DNS answer that lands just before its deadline is followed by a full dial deadline,
    so the bound this function advertises is quietly worth twice as much to a hostile or merely
    broken feed endpoint."""
    deadline = clock() + timeout          # armed before the lookup: DNS and the dials share it
    addresses = resolve_endpoints(address, port, timeout)
    start = clock()                       # after the lookup: latency measures the dial, not DNS
    remaining = deadline - start
    for dial_ip in addresses:
        if remaining <= 0:
            break
        try:
            conn = connect((dial_ip, port), remaining)
            conn.close()
        except OSError:
            remaining = deadline - clock()
            continue
        return True, int((clock() - start) * 1000)
    return False, None


def http_ping(address: str, port: int, sni: str, timeout: float = 5.0,
              connect=None, clock=time.monotonic) -> tuple[bool, int | None]:
    """HTTPS reachability probe — time a TLS handshake to the node endpoint with the
    given SNI (does the server answer at the TLS/HTTP layer). Returns ``(ok, latency_ms)``;
    None on failure. This is a DIRECT probe to address:port, not through the tunnel
    (xray has a single active outbound, so per-node through-tunnel probing isn't possible).
    Certs aren't verified — reality nodes present borrowed certs; we only time the handshake.

    The socket goes to the pinned IP; SNI stays the hostname the node declared, so a
    legitimately CNAME'd endpoint still presents the name its certificate is issued for.

    Like `tcp_ping`, every validated address is tried in turn under the one ``timeout``, and that
    one timeout also covers the lookup (see there) — a multi-record endpoint stays healthy while
    any of its addresses answers, without any of it costing more wall clock than advertised."""
    deadline = clock() + timeout          # armed before the lookup: DNS and the handshakes share it
    addresses = resolve_endpoints(address, port, timeout)
    if not addresses:
        return False, None
    if connect is None:
        def connect(addr, to):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx.wrap_socket(socket.create_connection(addr, to), server_hostname=sni or address)
    start = clock()
    remaining = deadline - start
    for dial_ip in addresses:
        if remaining <= 0:
            break
        try:
            conn = connect((dial_ip, port), remaining)
            conn.close()
        except OSError:              # ssl.SSLError subclasses OSError
            remaining = deadline - clock()
            continue
        return True, int((clock() - start) * 1000)
    return False, None


def _valid_ip(s: str | None) -> str | None:
    """Strict gate (audit B5): only a parseable IPv4/IPv6 literal may be stored as an
    egress IP — anything else (HTML error pages, hex-looking junk) becomes None instead
    of leaking into the UI / flag lookup."""
    if not s:
        return None
    try:
        ipaddress.ip_address(s)
    except ValueError:
        return None
    return s


def _parse_egress_ip(body: str) -> str | None:
    """Best-effort egress-IP extraction from common IP-echo responses: JSON
    (``{"ip": …}`` / ``{"origin": …}``), Cloudflare ``ip=…`` trace lines, or a
    bare-IP body. Every candidate is validated as a real IP literal."""
    body = body.strip()
    if not body:
        return None
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            for key in ("ip", "origin", "query"):
                if data.get(key):
                    return _valid_ip(str(data[key]).split(",")[0].strip())
    except (ValueError, TypeError):
        pass
    for line in body.splitlines():
        if line.startswith("ip="):
            return _valid_ip(line[3:].strip())
    return _valid_ip(body.split()[0])


def _dup_handle(sock):
    """A second descriptor for the same kernel socket, or None for an object that has none to
    give (a test double, anything that is not a real socket).

    ``ssl.SSLContext.wrap_socket`` *detaches* the socket it wraps: the object handed to it is
    left at fd -1 while the new ``SSLSocket`` takes the descriptor over. A guard armed with the
    raw socket therefore holds a dead handle for the whole TLS handshake — the one blocking
    step the deadline most needs to cover. A duplicate survives the detach, and ``shutdown`` on
    a duplicate reaches the same kernel socket, so it still wakes the read blocked on the TLS
    side.

    A real socket that *refuses* to duplicate is a different case, and it is deliberately not
    caught: ``dup`` fails under FD pressure, exactly when the box is loaded and the probe most
    needs its deadline, and falling back to the raw socket there would leave the handshake
    covered by nothing while the caller still believes the bound holds. A guarantee that
    degrades silently is worse than one that is absent, so the ``OSError`` propagates and the
    probe fails instead. Only a missing ``dup`` (``AttributeError`` — not a real socket, so
    there is no detach to survive) falls back to arming the object itself."""
    try:
        return sock.dup()
    except AttributeError:
        return None


def _guarded_connection(base, guard):
    """An ``http.client`` connection class that keeps ``guard`` armed on a handle that can
    actually stop the request at every blocking step.

    While the connection is being built (connect, proxy CONNECT, TLS handshake) that handle is
    a duplicate descriptor, because the TLS wrap detaches the socket ``http.client`` created —
    arming the raw socket alone leaves the guard holding fd -1 while the live TLS socket blocks
    on. Once ``connect()`` returns, the guard is re-armed on the socket the request will
    actually read from (the ``SSLSocket`` on https) and the duplicate is released.

    A socket that cannot be duplicated at all takes the connection down with it (see
    `_dup_handle`): the alternative is a request that runs on with a weaker deadline than the
    caller was promised and no sign that it did."""

    class _GuardedConnection(base):
        def connect(self):
            create = self._create_connection    # documented http.client seam
            mirror = None

            def _create(*a, **kw):
                nonlocal mirror
                sock = create(*a, **kw)
                try:
                    mirror = _dup_handle(sock)
                    guard.arm(mirror if mirror is not None else sock)
                except BaseException:
                    sock.close()
                    raise
                return sock

            self._create_connection = _create
            try:
                super().connect()
                guard.arm(self.sock)
            finally:
                self._create_connection = create
                if mirror is not None:
                    mirror.close()      # only the duplicate; the live descriptor stays open

    return _GuardedConnection


class _GuardedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, guard):
        super().__init__()
        self._guard = guard

    def http_open(self, req):
        return self.do_open(_guarded_connection(http.client.HTTPConnection, self._guard), req)


class _GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, guard):
        super().__init__()
        self._guard = guard

    def https_open(self, req):
        return self.do_open(_guarded_connection(http.client.HTTPSConnection, self._guard), req,
                            context=self._context)


def real_request(proxy_url: str, probe_url: str, timeout: float = 5.0,
                 opener_factory=None, clock=time.monotonic
                 ) -> tuple[bool, int | None, int | None, str | None]:
    """HTTPS GET to ``probe_url`` through the local http proxy at ``proxy_url`` (the
    active node's ``sub-fetch`` inbound). Returns ``(ok, status, latency_ms, egress_ip)``;
    all-None/False on any failure. ``ok`` is true only for a 2xx/3xx status.

    ``timeout`` is a HARD wall-clock bound. urllib's own timeout is a per-socket *idle* timer,
    so a server that dribbles a byte at a time — TLS records, header bytes, body fragments —
    resets it forever and pins the caller indefinitely; here that caller is the single liveness
    worker, and with it the xray watchdog and auto-failover. A deadline guard (the subscription
    fetcher's, same mechanism) shuts the live socket down when the wall clock runs out, and the
    check after the read makes sure a body that only ended *because* the guard cut the socket
    is reported as a failure rather than as a short answer."""
    guard = _fetcher()._DeadlineGuard(time.monotonic() + timeout)
    if opener_factory is None:
        def opener_factory():
            handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            return urllib.request.build_opener(handler, _GuardedHTTPHandler(guard),
                                               _GuardedHTTPSHandler(guard))

    start = clock()
    try:
        opener = opener_factory()
        with opener.open(probe_url, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            body = resp.read(_PROBE_BODY_CAP).decode("utf-8", "replace")
        guard.check()
    except Exception:
        return False, None, None, None
    finally:
        guard.cancel()
    latency_ms = int((clock() - start) * 1000)
    return (200 <= status < 400), status, latency_ms, _parse_egress_ip(body)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _probe_outbound(node, dial_ip: str | None = None) -> dict:
    """The node's vless proxy outbound — mirrors xray_config.builder (transport/security
    aware) but without the tproxy egress mark or tuning profile (a clean probe path).

    ``dial_ip`` is the address validated by `resolve_endpoint`; xray must dial exactly that,
    or the throwaway instance would resolve the hostname a second time and undo the check.
    SNI therefore has to carry the original hostname explicitly: with no ``sni`` on the node
    xray falls back to the vnext address, which would now put a bare IP on the wire."""
    user = {"id": node.uuid, "encryption": "none"}
    if node.flow:
        user["flow"] = node.flow
    network = node.network or "tcp"
    security = node.security or "reality"
    server_name = node.sni or node.address
    stream: dict = {"network": network, "security": security}
    if security == "reality":
        stream["realitySettings"] = {"serverName": server_name, "fingerprint": node.fingerprint,
                                     "publicKey": node.public_key, "shortId": node.short_id}
    else:
        tls: dict = {"serverName": server_name, "fingerprint": node.fingerprint}
        if node.alpn:
            tls["alpn"] = [a.strip() for a in node.alpn.split(",") if a.strip()]
        stream["tlsSettings"] = tls
    if network == "xhttp":
        stream["xhttpSettings"] = {k: getattr(node, k) for k in ("path", "host", "mode") if getattr(node, k)}
    return {"tag": "proxy", "protocol": "vless",
            "settings": {"vnext": [{"address": dial_ip or node.address, "port": node.port,
                                    "users": [user]}]},
            "streamSettings": stream}


def real_through_node(node, xray_bin: str, probe_url: str, timeout: float = 8.0,
                      spawn=None, wait_ready=None, probe_url6: str | None = None
                      ) -> tuple[bool, int | None, str | None, str | None]:
    """Spin up a throwaway xray (local http inbound + `node` as outbound), do a real request
    through it, then tear it down — so ANY node can be probed without touching the live tunnel.
    Returns ``(ok, latency_ms, egress_ip, egress_ip6)``; ``egress_ip6`` is the v6 egress when
    ``probe_url6`` (a v6-only echo) is given and the node carries v6, else None. ``spawn`` /
    ``wait_ready`` are injectable for tests.

    This one is the sharpest edge of the feed-controlled-endpoint problem: it does not just
    open a socket, it stands up a real xray outbound to the address the feed chose — so the
    validated IP is pinned into the generated config, not the hostname xray would re-resolve.

    Unlike the socket probes this one stays on the first validated address: retrying the others
    would mean a fresh xray process, its readiness wait and another full ``timeout`` each, and
    it is the cheap `tcp_ping`/`http_ping` sweep that failover actually reads."""
    dial_ip = resolve_endpoint(getattr(node, "address", None), getattr(node, "port", 443) or 443)
    if dial_ip is None:
        return False, None, None, None
    with _PROBE_SEM:
        return _real_through_node(node, xray_bin, probe_url, timeout, spawn, wait_ready,
                                  probe_url6, dial_ip)


def _real_through_node(node, xray_bin, probe_url, timeout, spawn, wait_ready, probe_url6=None,
                       dial_ip=None):
    port = _free_port()
    cfg = {
        "log": {"loglevel": "warning"},
        "inbounds": [{"tag": "in", "protocol": "http", "listen": "127.0.0.1", "port": port, "settings": {}}],
        "outbounds": [_probe_outbound(node, dial_ip), {"tag": "direct", "protocol": "freedom"}],
    }
    if spawn is None:
        def spawn(config_path):
            return subprocess.Popen([xray_bin, "-config", config_path],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if wait_ready is None:
        def wait_ready(p):
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                try:
                    socket.create_connection(("127.0.0.1", p), 0.2).close()
                    return
                except OSError:
                    time.sleep(0.1)
    fd, path = tempfile.mkstemp(suffix=".json")
    proc = None
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f)
        proc = spawn(path)
        wait_ready(port)
        proxy = f"http://127.0.0.1:{port}"
        ok, _status, ms, egress = real_request(proxy, probe_url, timeout=timeout)
        egress6 = real_request(proxy, probe_url6, timeout=timeout)[3] if probe_url6 else None
        return ok, ms, egress, egress6
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        try:
            os.unlink(path)
        except OSError:
            pass
