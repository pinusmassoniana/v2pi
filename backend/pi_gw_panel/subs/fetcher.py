import urllib.request
from pi_gw_panel.subs.inject import build_request


def _http_get(url: str, headers: dict, proxy: str | None, timeout: float) -> str:
    """Stdlib GET with optional HTTP proxy. Isolated so tests can stub it; the real
    urllib path runs against the tunnel proxy on the Pi (Plan 8)."""
    proxies = {"http": proxy, "https": proxy} if proxy else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    request = urllib.request.Request(url, headers=headers, method="GET")
    with opener.open(request, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, "replace")


def fetch(url: str, injection: dict, tokens: dict, *, proxy: str | None) -> tuple[str, str]:
    """GET the subscription. If `proxy` (e.g. 'http://127.0.0.1:10808') is set, route through it
    (tunnel); else direct. Returns (body, path) with path in {'tunnel', 'direct'}."""
    req = build_request(url, injection, tokens)
    path = "tunnel" if proxy else "direct"
    body = _http_get(req.url, req.headers, proxy, 20.0)
    return body, path
