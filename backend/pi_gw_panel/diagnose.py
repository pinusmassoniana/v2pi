"""B3: what a node's connection actually looks like right now, on demand.

The health probe answers one question — did a real request come back? — and that is the right
question for a watchdog. It is the wrong one for "the tunnel connects and then everything
crawls", which is what throttling looks like from the inside: the handshake succeeds, the first
bytes arrive, and the stream dies a few hundred kilobytes in. This measures the phases
separately so the difference is visible:

    TCP connect → TLS handshake → first byte through the tunnel → a bounded transfer

Everything here is heuristic and says so. A stalled transfer is consistent with DPI throttling;
it is also consistent with a busy server or a bad Wi-Fi link. The panel reports what it measured
and what that is consistent with — never "you are being throttled".

Never scheduled (D4): one diagnosis at a time, started by a person, with a hard overall bound.
"""
import http.client
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

from pi_gw_panel.health.probe import (_GuardedHTTPHandler, _GuardedHTTPSHandler, _fetcher,
                                      _free_port, _probe_outbound, _wait_ready, resolve_endpoint)
from pi_gw_panel.proc import stop_process

logger = logging.getLogger(__name__)

# One at a time, and never inside `apply_lock`: a diagnosis reads, changes nothing, and must not
# be able to block a connect — or be blocked by one.
lock = threading.Lock()

TOTAL_BUDGET = 30.0        # hard wall-clock bound for the whole run
CONNECT_TIMEOUT = 6.0
MAX_BYTES_CEILING = 1_048_576
CHUNK = 32_768

# Where "slow" starts. Deliberately low: the point is to separate "works" from "crawls", not to
# grade a link. A gateway on a 4G uplink is not a throttled one.
SLOW_KBPS = 500
SLOW_TTFB_MS = 2_000

VERDICTS = ("ok", "slow", "stalls", "down")


def _tcp_ms(address: str, port: int, timeout: float) -> tuple[int | None, str]:
    start = time.monotonic()
    try:
        socket.create_connection((address, port), timeout).close()
    except OSError as exc:
        return None, str(exc)
    return int((time.monotonic() - start) * 1000), ""


def _tls_ms(address: str, port: int, server_name: str, timeout: float) -> tuple[int | None, str]:
    """A plain TLS handshake to the node, with the node's own SNI.

    Not a certificate check — Reality answers with the camouflage site's certificate and a
    Trojan server with whatever it fronts, so verifying anything here would fail on a healthy
    node. What is being measured is whether the handshake completes at all: a connection that
    accepts TCP and then dies on the ClientHello is the shape SNI-based blocking leaves.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    start = time.monotonic()
    try:
        with socket.create_connection((address, port), timeout) as raw:
            with context.wrap_socket(raw, server_hostname=server_name or None):
                pass
    except (OSError, ssl.SSLError) as exc:
        return None, str(exc)
    return int((time.monotonic() - start) * 1000), ""


def _transfer(proxy: str, url: str, max_bytes: int, deadline: float) -> dict:
    """One bounded GET through the throwaway instance, timed in two halves: to the first byte,
    and then for as much of the body as the budget and `max_bytes` allow."""
    out: dict = {"ttfb_ms": None, "bytes": 0, "transfer_ms": 0, "kbps": None,
                 "stalled": False, "error": ""}
    # `timeout=` below is urllib's per-recv IDLE timer: a peer that drips a byte at a time — which
    # is what throttling looks like — resets it forever, and one 32 KiB read outlasts the whole
    # budget. The guard makes TOTAL_BUDGET hard by shutting the live socket down at `deadline`.
    guard = _fetcher()._DeadlineGuard(deadline)
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    opener = urllib.request.build_opener(handler, _GuardedHTTPHandler(guard),
                                         _GuardedHTTPSHandler(guard))
    start = time.monotonic()
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            out["error"] = "out of time before the request started"
            return out
        with opener.open(url, timeout=remaining) as response:
            out["ttfb_ms"] = int((time.monotonic() - start) * 1000)
            length = response.headers.get("Content-Length")
            try:
                want = min(max_bytes, int(length)) if length else max_bytes
            except ValueError:
                want = max_bytes
            body_start = time.monotonic()
            received = 0
            try:
                while received < want and time.monotonic() < deadline:
                    chunk = response.read(min(CHUNK, want - received))
                    if not chunk:
                        break
                    received += len(chunk)
            except (OSError, http.client.HTTPException):
                if not guard.expired:
                    raise              # a real failure, not the deadline cutting the socket
            out["bytes"] = received
            out["transfer_ms"] = int((time.monotonic() - body_start) * 1000)
            # Short of what the server said it would send is the signal: the stream started and
            # then stopped. A body that simply IS smaller than `max_bytes` is not a stall, which
            # is why `want` comes from Content-Length when there is one.
            out["stalled"] = received < want
            if out["transfer_ms"] > 0 and received:
                out["kbps"] = int(received * 8 / out["transfer_ms"])
    except Exception as exc:                      # noqa: BLE001 — any failure is an answer here
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        guard.cancel()
    return out


def classify(tcp_ms, tls_error: str, transfer: dict) -> tuple[str, str]:
    """`(verdict, one sentence)`. The sentence is the whole point: a verdict without what it was
    measured from is a number the operator has to trust rather than read."""
    if tcp_ms is None:
        return "down", "the gateway cannot even open a TCP connection to this node."
    if transfer["error"] or transfer["ttfb_ms"] is None:
        if tls_error:
            return "down", ("TCP connects, but the TLS handshake does not complete — the shape "
                            "SNI-based blocking leaves, and also what a wrong port or a dead "
                            "server looks like.")
        return "down", ("TCP connects and the handshake completes, but nothing came back "
                        "through the tunnel — consistent with blocking after the handshake, "
                        "and with wrong credentials.")
    if transfer["stalled"]:
        return "stalls", (f"the handshake is fine and the first byte arrived, then the stream "
                          f"stopped after {transfer['bytes'] // 1024} KB — the shape throttling "
                          "leaves, and also what a busy server or a lossy uplink looks like.")
    if (transfer["kbps"] is not None and transfer["kbps"] < SLOW_KBPS) or \
            (transfer["ttfb_ms"] or 0) > SLOW_TTFB_MS:
        return "slow", "the transfer completed, but slowly enough to be worth comparing against another node."
    return "ok", "connects, hands over the first byte quickly and carries a full transfer."


def run(node, xray_bin: str, url: str, max_bytes: int, budget: float = TOTAL_BUDGET,
        spawn=None, wait_ready=None) -> dict:
    """Measure one node. Raises nothing the caller has to catch: every failure is a phase that
    did not happen, and the verdict says which."""
    deadline = time.monotonic() + budget
    max_bytes = max(1, min(int(max_bytes), MAX_BYTES_CEILING))
    dial_ip = resolve_endpoint(node.address, node.port, timeout=CONNECT_TIMEOUT)
    if dial_ip is None:
        return {"verdict": "down", "detail": "this node's address does not resolve to an "
                                             "address the gateway is allowed to dial.",
                "tcp_ms": None, "tls_ms": None, "ttfb_ms": None, "bytes": 0, "kbps": None,
                "transfer_ms": 0, "requested_bytes": max_bytes, "error": "", "url": url}

    tcp_ms, tcp_error = _tcp_ms(dial_ip, node.port, CONNECT_TIMEOUT)
    tls_ms, tls_error = (None, "")
    # Shadowsocks has no TLS layer of its own — there is no handshake to time, and reporting a
    # failed one would be an invented symptom.
    if tcp_ms is not None and node.security in ("tls", "reality"):
        tls_ms, tls_error = _tls_ms(dial_ip, node.port, node.sni or node.address, CONNECT_TIMEOUT)

    transfer = {"ttfb_ms": None, "bytes": 0, "transfer_ms": 0, "kbps": None,
                "stalled": False, "error": tcp_error or "not attempted"}
    if tcp_ms is not None:
        transfer = _through_node(node, xray_bin, url, max_bytes, dial_ip, deadline,
                                 spawn, wait_ready)
    verdict, detail = classify(tcp_ms, tls_error, transfer)
    return {"verdict": verdict, "detail": detail, "tcp_ms": tcp_ms, "tls_ms": tls_ms,
            "ttfb_ms": transfer["ttfb_ms"], "bytes": transfer["bytes"],
            "transfer_ms": transfer["transfer_ms"], "kbps": transfer["kbps"],
            "requested_bytes": max_bytes, "error": transfer["error"] or tls_error, "url": url}


def _through_node(node, xray_bin, url, max_bytes, dial_ip, deadline, spawn=None,
                  wait_ready=None) -> dict:
    """The throwaway xray, same shape the health probe uses: one http inbound on loopback, the
    node's own outbound, nothing else. No tuning profile and no egress mark — this measures the
    node, not the gateway's routing."""
    port = _free_port()
    cfg = {
        "log": {"loglevel": "warning"},
        "inbounds": [{"tag": "in", "protocol": "http", "listen": "127.0.0.1", "port": port,
                      "settings": {}}],
        "outbounds": [_probe_outbound(node, dial_ip), {"tag": "direct", "protocol": "freedom"}],
    }
    if spawn is None:
        def spawn(config_path):
            return subprocess.Popen([xray_bin, "-config", config_path],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if wait_ready is None:
        def wait_ready(p):
            _wait_ready(p, deadline)
    fd, path = tempfile.mkstemp(suffix=".json")
    proc = None
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(cfg, handle)
        proc = spawn(path)
        wait_ready(port)
        return _transfer(f"http://127.0.0.1:{port}", url, max_bytes, deadline)
    except Exception as exc:                      # noqa: BLE001 — a spawn failure is an answer
        return {"ttfb_ms": None, "bytes": 0, "transfer_ms": 0, "kbps": None, "stalled": False,
                "error": f"{type(exc).__name__}: {exc}"}
    finally:
        # Teardown inside the same budget, for the reason the probe's does: a throwaway instance
        # must never outlive the bound the caller was promised.
        if not stop_process(proc, budget=max(0.0, deadline - time.monotonic()), grace=1.0,
                            reap=1.0, name="diagnose xray"):
            logger.error("diagnose teardown leaked an xray for %s:%s", node.address, node.port)
        try:
            os.unlink(path)
        except OSError:
            pass
