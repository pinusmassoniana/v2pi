from pi_gw_panel.health.probe import tcp_ping, real_request


class _FakeConn:
    def close(self):
        pass


def test_tcp_ping_ok_via_injected_connect():
    times = iter([0.0, 0.05])                       # 50 ms
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
