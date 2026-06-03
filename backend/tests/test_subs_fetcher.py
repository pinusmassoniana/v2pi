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
