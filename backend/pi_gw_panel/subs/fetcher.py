import http.cookiejar
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from pi_gw_panel.subs.inject import build_request

ALLOWED_SCHEMES = ("http", "https")
ALLOW_LOOPBACK = False   # test seam: integration tests fetch from a local stub server
MAX_BYTES = 5 * 1024 * 1024   # cap the in-memory body so a hostile/huge endpoint can't OOM the Pi


def _ip_blocked(addr) -> bool:
    return (addr.is_loopback or addr.is_private or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def host_blocked(host: str) -> bool:
    """SSRF guard: True if `host` IS or RESOLVES TO a loopback/private/link-local/reserved address
    (169.254.169.254 cloud-metadata, 10/8, 192.168/16, ::1, fc00::/7, …). The panel runs as root
    on the gateway between LAN and WAN, so a subscription URL must never reach an internal host.
    Applied to the admin URL AND every redirect hop. Bypassed by ALLOW_LOOPBACK (tests)."""
    if ALLOW_LOOPBACK:
        return False
    host = (host or "").strip("[]").lower()
    if not host or host == "localhost":
        return True
    try:                                  # literal IP?
        return _ip_blocked(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:                                  # DNS name → reject if ANY resolved address is internal
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True                       # unresolvable → refuse rather than let urllib try
    for ai in infos:
        try:
            if _ip_blocked(ipaddress.ip_address(ai[4][0])):
                return True
        except ValueError:
            continue
    return False


def assert_public_url(url: str) -> None:
    """Raise ValueError unless `url` is an http(s) URL whose host is public (not internal). Shared
    entry point so the API preview/refresh handlers get the same SSRF vetting as the fetcher."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError(f"unsupported URL scheme '{parts.scheme or '(none)'}': only http/https allowed")
    if host_blocked(parts.hostname or ""):
        raise ValueError("subscription URL resolves to a non-public (internal) address")


class _SchemeGuardRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects (needed for the cookie-challenge gate) but refuse to leave http/https OR
    to hop to an internal host — so a 302 → file:// (scheme) or 302 → http://169.254.169.254/
    (host) can't turn the fetcher into a local-file/SSRF read (the host check must repeat per hop,
    not just on the admin-entered URL)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme.lower() not in ALLOWED_SCHEMES:
            raise urllib.error.HTTPError(newurl, code, "redirect to disallowed scheme", headers, fp)
        if host_blocked(parts.hostname or ""):
            raise urllib.error.HTTPError(newurl, code, "redirect to non-public host", headers, fp)
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
    # Full SSRF vetting (scheme + host resolves-to-public): a subscription points at a provider,
    # never at this host or the LAN/metadata endpoints (audit B6). Repeated per redirect hop below.
    assert_public_url(req.url)
    path = "tunnel" if proxy else "direct"
    body, resp_headers = _http_get(req.url, req.headers, proxy, 20.0)
    return body, path, resp_headers
