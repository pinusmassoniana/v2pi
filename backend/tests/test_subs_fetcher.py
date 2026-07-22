import http.server
import socket
import threading
import time

import pytest

from pi_gw_panel.subs import fetcher
from pi_gw_panel.subs.fetcher import fetch


def test_fetch_direct_builds_request_and_no_proxy(monkeypatch):
    calls = {}

    def fake(url, headers, proxy, timeout):
        calls.update(url=url, headers=headers, proxy=proxy, timeout=timeout)
        return "BODY", {}

    monkeypatch.setattr(fetcher, "_http_get", fake)
    monkeypatch.setattr(fetcher, "assert_public_url", lambda url: None)   # this test exercises plumbing, not the SSRF guard
    body, path, headers = fetch("https://h/s", {"headers": {"x-h": "1"}}, {}, proxy=None)
    assert body == "BODY" and path == "direct" and headers == {}
    assert calls["proxy"] is None
    assert calls["headers"]["x-h"] == "1"
    assert calls["url"] == "https://h/s"


def test_fetch_tunnel_passes_proxy(monkeypatch):
    calls = {}

    def fake(url, headers, proxy, timeout):
        calls["proxy"] = proxy
        return "B", {}

    monkeypatch.setattr(fetcher, "_http_get", fake)
    monkeypatch.setattr(fetcher, "assert_public_url", lambda url: None)   # plumbing test, not SSRF
    body, path, _ = fetch("https://h/s", {}, {}, proxy="http://127.0.0.1:10808")
    assert path == "tunnel"
    assert calls["proxy"] == "http://127.0.0.1:10808"


def test_fetch_rejects_non_http_scheme(monkeypatch):
    # S1: a file:///ftp:// URL must be refused before any network/file access.
    monkeypatch.setattr(fetcher, "_http_get", lambda *a, **k: ("SHOULD-NOT-RUN", {}))
    for url in ("file:///etc/passwd", "ftp://host/x"):
        with pytest.raises(ValueError):
            fetch(url, {}, {}, proxy=None)


class _CookieChallengeHandler(http.server.BaseHTTPRequestHandler):
    """First GET with no cookie → 302 + Set-Cookie, redirecting to the same path
    (the anti-bot challenge v2rayA-style providers use). Second GET that echoes the
    cookie → 200 + body. Mirrors subs.eu-fffast.com's __hash_ gate."""

    def do_GET(self):
        if "pass=1" not in (self.headers.get("Cookie") or ""):
            self.send_response(302)
            self.send_header("Set-Cookie", "pass=1; Path=/")
            self.send_header("Location", self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"vless://node"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep test output pristine
        pass


def test_http_get_follows_cookie_challenge_redirect(monkeypatch):
    # loopback stub server: the SSRF redirect-host guard must be relaxed (the documented test seam)
    monkeypatch.setattr(fetcher, "ALLOW_LOOPBACK", True)
    server = http.server.HTTPServer(("127.0.0.1", 0), _CookieChallengeHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body, headers = fetcher._http_get(f"http://127.0.0.1:{port}/sub", {}, None, 5.0)
    finally:
        server.shutdown()
        thread.join()
    assert body == "vless://node"
    assert isinstance(headers, dict)


def test_resolve_public_returns_pinned_ip_and_rejects_mixed_answer(monkeypatch):
    infos = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: infos)
    assert fetcher._resolve_public("example.com", 443, time.monotonic() + 1) == "93.184.216.34"

    infos.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)))
    with pytest.raises(ValueError, match="non-public"):
        fetcher._resolve_public("example.com", 443, time.monotonic() + 1)


def test_http_get_repins_every_redirect_and_preserves_host(monkeypatch):
    resolved = []
    calls = []

    def resolve(host, port, deadline):
        resolved.append((host, port))
        return {"one.example": "93.184.216.34", "two.example": "93.184.216.35"}[host]

    def request(parts, pinned_ip, headers, proxy, deadline):
        calls.append((parts.hostname, pinned_ip, headers.get("Host")))
        if parts.hostname == "one.example":
            return 302, {"Location": "https://two.example/final"}, b""
        return 200, {"Content-Type": "text/plain; charset=utf-8"}, b"ok"

    monkeypatch.setattr(fetcher, "_resolve_public", resolve)
    monkeypatch.setattr(fetcher, "_request_once", request)
    body, _headers = fetcher._http_get("https://one.example/start", {"x-test": "1"}, None, 1)
    assert body == "ok"
    assert resolved == [("one.example", 443), ("two.example", 443)]
    assert calls == [
        ("one.example", "93.184.216.34", "one.example"),
        ("two.example", "93.184.216.35", "two.example"),
    ]


def test_remaining_uses_one_monotonic_deadline():
    with pytest.raises(TimeoutError, match="deadline"):
        fetcher._remaining(time.monotonic() - 0.01)


def test_dns_resolution_obeys_fetch_deadline(monkeypatch):
    def slow_resolve(*args, **kwargs):
        time.sleep(0.05)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", slow_resolve)
    with pytest.raises(TimeoutError, match="DNS deadline"):
        fetcher._resolve_public("slow.example", 443, time.monotonic() + 0.005)


def test_proxy_connect_targets_only_the_pinned_ip():
    class FakeSocket:
        sent = b""

        def settimeout(self, _timeout):
            pass

        def sendall(self, value):
            self.sent += value

        def recv(self, _size):
            return b"HTTP/1.1 200 Connection established\r\n\r\n"

    sock = FakeSocket()
    fetcher._proxy_connect(sock, "2001:4860:4860::8888", 443, time.monotonic() + 1)
    assert sock.sent.startswith(b"CONNECT [2001:4860:4860::8888]:443 HTTP/1.1\r\n")
    assert b"Host: [2001:4860:4860::8888]:443\r\n" in sock.sent
