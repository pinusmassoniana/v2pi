"""Network probes for the health subsystem (stdlib socket + urllib).

Both functions are blocking I/O, designed to be offloaded to a thread by the
monitor and fully stubbed in tests via the injected `connect` / `opener_factory`
/ `clock` seams — no real network is touched on the dev host. Real probing is
exercised on the Pi (Plan 8)."""
import json
import os
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.request

_PROBE_BODY_CAP = 64_000   # NS1: IP-echo responses are tiny; cap so a huge body can't OOM
# NR4: bound how many throwaway-xray probes can run at once (per-node "T" / real-test-all)
_PROBE_SEM = threading.BoundedSemaphore(3)


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
            body = resp.read(_PROBE_BODY_CAP).decode("utf-8", "replace")
    except Exception:
        return False, None, None, None
    latency_ms = int((clock() - start) * 1000)
    return (200 <= status < 400), status, latency_ms, _parse_egress_ip(body)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _probe_outbound(node) -> dict:
    """The node's vless proxy outbound — mirrors xray_config.builder (transport/security
    aware) but without the tproxy egress mark or tuning profile (a clean probe path)."""
    user = {"id": node.uuid, "encryption": "none"}
    if node.flow:
        user["flow"] = node.flow
    network = node.network or "tcp"
    security = node.security or "reality"
    stream: dict = {"network": network, "security": security}
    if security == "reality":
        stream["realitySettings"] = {"serverName": node.sni, "fingerprint": node.fingerprint,
                                     "publicKey": node.public_key, "shortId": node.short_id}
    else:
        tls: dict = {"serverName": node.sni, "fingerprint": node.fingerprint}
        if node.alpn:
            tls["alpn"] = [a.strip() for a in node.alpn.split(",") if a.strip()]
        stream["tlsSettings"] = tls
    if network == "xhttp":
        stream["xhttpSettings"] = {k: getattr(node, k) for k in ("path", "host", "mode") if getattr(node, k)}
    return {"tag": "proxy", "protocol": "vless",
            "settings": {"vnext": [{"address": node.address, "port": node.port, "users": [user]}]},
            "streamSettings": stream}


def real_through_node(node, xray_bin: str, probe_url: str, timeout: float = 8.0,
                      spawn=None, wait_ready=None, probe_url6: str | None = None
                      ) -> tuple[bool, int | None, str | None, str | None]:
    """Spin up a throwaway xray (local http inbound + `node` as outbound), do a real request
    through it, then tear it down — so ANY node can be probed without touching the live tunnel.
    Returns ``(ok, latency_ms, egress_ip, egress_ip6)``; ``egress_ip6`` is the v6 egress when
    ``probe_url6`` (a v6-only echo) is given and the node carries v6, else None. ``spawn`` /
    ``wait_ready`` are injectable for tests."""
    with _PROBE_SEM:
        return _real_through_node(node, xray_bin, probe_url, timeout, spawn, wait_ready, probe_url6)


def _real_through_node(node, xray_bin, probe_url, timeout, spawn, wait_ready, probe_url6=None):
    port = _free_port()
    cfg = {
        "log": {"loglevel": "warning"},
        "inbounds": [{"tag": "in", "protocol": "http", "listen": "127.0.0.1", "port": port, "settings": {}}],
        "outbounds": [_probe_outbound(node), {"tag": "direct", "protocol": "freedom"}],
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
