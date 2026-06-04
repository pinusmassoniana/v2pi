import http.server
import threading

from pi_gw_panel.subs import fetcher
from pi_gw_panel.subs.fetcher import fetch


def test_fetch_direct_builds_request_and_no_proxy(monkeypatch):
    calls = {}

    def fake(url, headers, proxy, timeout):
        calls.update(url=url, headers=headers, proxy=proxy, timeout=timeout)
        return "BODY"

    monkeypatch.setattr(fetcher, "_http_get", fake)
    body, path = fetch("https://h/s", {"headers": {"x-h": "1"}}, {}, proxy=None)
    assert body == "BODY" and path == "direct"
    assert calls["proxy"] is None
    assert calls["headers"]["x-h"] == "1"
    assert calls["url"] == "https://h/s"


def test_fetch_tunnel_passes_proxy(monkeypatch):
    calls = {}

    def fake(url, headers, proxy, timeout):
        calls["proxy"] = proxy
        return "B"

    monkeypatch.setattr(fetcher, "_http_get", fake)
    body, path = fetch("https://h/s", {}, {}, proxy="http://127.0.0.1:10808")
    assert path == "tunnel"
    assert calls["proxy"] == "http://127.0.0.1:10808"


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


def test_http_get_follows_cookie_challenge_redirect():
    server = http.server.HTTPServer(("127.0.0.1", 0), _CookieChallengeHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = fetcher._http_get(f"http://127.0.0.1:{port}/sub", {}, None, 5.0)
    finally:
        server.shutdown()
        thread.join()
    assert body == "vless://node"
