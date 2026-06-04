"""Network probes for the health subsystem (stdlib socket + urllib).

Both functions are blocking I/O, designed to be offloaded to a thread by the
monitor and fully stubbed in tests via the injected `connect` / `opener_factory`
/ `clock` seams — no real network is touched on the dev host. Real probing is
exercised on the Pi (Plan 8)."""
import json
import socket
import ssl
import time
import urllib.request


def tcp_ping(address: str, port: int, timeout: float = 3.0,
             connect=socket.create_connection, clock=time.monotonic) -> tuple[bool, int | None]:
    """TCP-connect liveness probe. Returns ``(ok, latency_ms)``; ``latency_ms`` is
    None when the connection fails (refused / timeout / unreachable)."""
    start = clock()
    try:
        conn = connect((address, port), timeout)
        conn.close()
    except OSError:
        return False, None
    return True, int((clock() - start) * 1000)


def http_ping(address: str, port: int, sni: str, timeout: float = 5.0,
              connect=None, clock=time.monotonic) -> tuple[bool, int | None]:
    """HTTPS reachability probe — time a TLS handshake to the node endpoint with the
    given SNI (does the server answer at the TLS/HTTP layer). Returns ``(ok, latency_ms)``;
    None on failure. This is a DIRECT probe to address:port, not through the tunnel
    (xray has a single active outbound, so per-node through-tunnel probing isn't possible).
    Certs aren't verified — reality nodes present borrowed certs; we only time the handshake."""
    if connect is None:
        def connect(addr, to):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx.wrap_socket(socket.create_connection(addr, to), server_hostname=sni or address)
    start = clock()
    try:
        conn = connect((address, port), timeout)
        conn.close()
    except OSError:                  # ssl.SSLError subclasses OSError
        return False, None
    return True, int((clock() - start) * 1000)


def _parse_egress_ip(body: str) -> str | None:
    """Best-effort egress-IP extraction from common IP-echo responses: JSON
    (``{"ip": …}`` / ``{"origin": …}``), Cloudflare ``ip=…`` trace lines, or a
    bare-IP body."""
    body = body.strip()
    if not body:
        return None
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            for key in ("ip", "origin", "query"):
                if data.get(key):
                    return str(data[key]).split(",")[0].strip()
    except (ValueError, TypeError):
        pass
    for line in body.splitlines():
        if line.startswith("ip="):
            return line[3:].strip()
    first = body.split()[0]
    if all(c in "0123456789abcdefABCDEF.:" for c in first):   # looks like an IPv4/IPv6 literal
        return first
    return None


def real_request(proxy_url: str, probe_url: str, timeout: float = 5.0,
                 opener_factory=None, clock=time.monotonic
                 ) -> tuple[bool, int | None, int | None, str | None]:
    """HTTPS GET to ``probe_url`` through the local http proxy at ``proxy_url`` (the
    active node's ``sub-fetch`` inbound). Returns ``(ok, status, latency_ms, egress_ip)``;
    all-None/False on any failure. ``ok`` is true only for a 2xx/3xx status."""
    if opener_factory is None:
        def opener_factory():
            handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            return urllib.request.build_opener(handler)

    start = clock()
    try:
        opener = opener_factory()
        with opener.open(probe_url, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            body = resp.read().decode("utf-8", "replace")
    except Exception:
        return False, None, None, None
    latency_ms = int((clock() - start) * 1000)
    return (200 <= status < 400), status, latency_ms, _parse_egress_ip(body)
