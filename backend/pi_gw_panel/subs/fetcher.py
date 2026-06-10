import http.cookiejar
import ipaddress
import urllib.error
import urllib.parse
import urllib.request
from pi_gw_panel.subs.inject import build_request

ALLOWED_SCHEMES = ("http", "https")
ALLOW_LOOPBACK = False   # test seam: integration tests fetch from a local stub server
MAX_BYTES = 5 * 1024 * 1024   # cap the in-memory body so a hostile/huge endpoint can't OOM the Pi


class _SchemeGuardRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects (needed for the cookie-challenge gate) but refuse to leave http/https,
    so a 302 → file:// (or other scheme) can't turn the fetcher into a local-file/SSRF read."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme.lower() not in ALLOWED_SCHEMES:
            raise urllib.error.HTTPError(newurl, code, "redirect to disallowed scheme", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _http_get(url: str, headers: dict, proxy: str | None, timeout: float) -> tuple[str, dict]:
    """Stdlib GET with optional HTTP proxy. Returns (body_text, response_headers).

    A fresh CookieJar is attached per request so anti-bot providers that gate the
    subscription behind a cookie challenge work: the first response is 302 + Set-Cookie
    redirecting to the same URL, and the jar carries that cookie into the retry. The body is
    read with a hard cap (MAX_BYTES) so an oversized response can't exhaust the Pi's memory."""
    proxies = {"http": proxy, "https": proxy} if proxy else {}
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(proxies),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        _SchemeGuardRedirect(),
    )
    request = urllib.request.Request(url, headers=headers, method="GET")
    with opener.open(request, timeout=timeout) as resp:
        raw = resp.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError(f"subscription body exceeds the {MAX_BYTES // (1024 * 1024)} MB cap")
        charset = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, "replace"), dict(resp.headers)


def fetch(url: str, injection: dict, tokens: dict, *, proxy: str | None) -> tuple[str, str, dict]:
    """GET the subscription. If `proxy` (e.g. 'http://127.0.0.1:10808') is set, route through it
    (tunnel); else direct. Returns (body, path, headers) with path in {'tunnel', 'direct'}.
    Only http/https URLs are accepted — file://, ftp:// etc. are rejected before any I/O."""
    req = build_request(url, injection, tokens)
    parts = urllib.parse.urlsplit(req.url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError(f"unsupported URL scheme '{parts.scheme or '(none)'}': only http/https allowed")
    # Defense-in-depth (audit B6): a subscription points at a provider, never at this host —
    # refuse loopback targets so the admin-entered URL can't be used to poke the panel's own
    # local-only services (stats gRPC, local proxy). Literal-IP / 'localhost' check only.
    host = (parts.hostname or "").strip("[]").lower()
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host == "localhost"
    if is_loopback and not ALLOW_LOOPBACK:
        raise ValueError("loopback subscription URLs are not allowed")
    path = "tunnel" if proxy else "direct"
    body, resp_headers = _http_get(req.url, req.headers, proxy, 20.0)
    return body, path, resp_headers
