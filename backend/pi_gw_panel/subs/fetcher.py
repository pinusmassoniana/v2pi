import concurrent.futures
import http.client
import http.cookies
import ipaddress
import socket
import ssl
import threading
import time
import urllib.parse

from pi_gw_panel.subs.inject import build_request

ALLOWED_SCHEMES = ("http", "https")
ALLOW_LOOPBACK = False   # test seam: integration tests fetch from a local stub server
MAX_BYTES = 1024 * 1024
MAX_REDIRECTS = 5
# Injected headers carry provider secrets (default_injection ships x-hwid; operators put
# subscription tokens there). Only these are safe to replay to a *different* origin after a
# redirect; everything else is dropped on a cross-origin hop.
SAFE_CROSS_ORIGIN_HEADERS = frozenset({"user-agent", "accept", "accept-language",
                                       "accept-encoding", "host", "connection"})
# A response only decodes with a charset we recognise; the label is attacker-controlled and
# codecs like `idna`/`punycode`/`zip` are reachable through str.decode by name.
ALLOWED_CHARSETS = frozenset({
    "utf-8", "utf8", "ascii", "us-ascii", "iso-8859-1", "iso8859-1", "latin-1", "latin1",
    "windows-1250", "cp1250", "windows-1251", "cp1251", "windows-1252", "cp1252",
    "utf-16", "utf-16le", "utf-16be", "gbk", "gb2312", "gb18030", "big5",
    "shift_jis", "sjis", "euc-jp", "euc-kr", "koi8-r",
})
_DNS_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4,
                                                   thread_name_prefix="sub-dns")


def _ip_blocked(addr) -> bool:
    # ALLOW_LOOPBACK is a *loopback-only* test seam: it must never wave through the rest of the
    # private ranges (RFC1918 / link-local / reserved), which is what the SSRF guard is for.
    if addr.is_loopback:
        return not ALLOW_LOOPBACK
    return (addr.is_private or addr.is_link_local or addr.is_reserved
            or addr.is_multicast or addr.is_unspecified)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("subscription fetch deadline exceeded")
    return remaining


def _resolve_public_all(host: str, port: int, deadline: float) -> list[str]:
    """Resolve one hop once, reject mixed public/private answers, and return **every** address
    the resolver gave, in its order.

    Callers that dial exactly one address take the first (see ``_resolve_public``); callers that
    can fail over between them (the health probes) walk the list. Either way the answer is
    resolved once and the whole answer is validated, so a second lookup can't slip a private
    address past the check.
    """
    host = (host or "").strip("[]").lower()
    if not host or host == "localhost":
        raise ValueError("subscription URL resolves to a non-public (internal) address")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        future = _DNS_POOL.submit(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
        try:
            infos = future.result(timeout=_remaining(deadline))
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise TimeoutError("subscription DNS deadline exceeded") from exc
        except OSError as exc:
            raise ValueError(f"subscription host could not be resolved: {exc}") from exc
        addresses = []
        for info in infos:
            try:
                address = ipaddress.ip_address(info[4][0])
            except (ValueError, IndexError):
                continue
            if address not in addresses:
                addresses.append(address)
    else:
        addresses = [literal]
    if not addresses:
        raise ValueError("subscription host could not be resolved")
    if any(_ip_blocked(address) for address in addresses):
        raise ValueError("subscription URL resolves to a non-public (internal) address")
    return [str(address) for address in addresses]


def _resolve_public(host: str, port: int, deadline: float) -> str:
    """Resolve one hop once, reject mixed public/private answers, and return the pinned IP."""
    return _resolve_public_all(host, port, deadline)[0]


def host_blocked(host: str) -> bool:
    """Compatibility helper used by validation callers; live fetch pins its own resolution."""
    try:
        _resolve_public(host, 443, time.monotonic() + 5)
    except (ValueError, TimeoutError):
        return True
    return False


def assert_public_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError(f"unsupported URL scheme '{parts.scheme or '(none)'}': only http/https allowed")
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise ValueError("invalid subscription URL port") from exc
    _resolve_public(parts.hostname or "", port, time.monotonic() + 5)


def _authority(host: str, port: int, scheme: str) -> str:
    rendered = f"[{host}]" if ":" in host and not host.startswith("[") else host
    default = 443 if scheme == "https" else 80
    return rendered if port == default else f"{rendered}:{port}"


def _header_items(headers) -> list[tuple[str, str]]:
    """Accept either a mapping or the raw ``[(name, value), …]`` list. Repeated headers
    (``Set-Cookie``, ``Subscription-Userinfo``) only survive in the list form — collapsing the
    response into a dict silently drops all but one of them."""
    if headers is None:
        return []
    if hasattr(headers, "items"):
        return [(str(key), str(value)) for key, value in headers.items()]
    return [(str(key), str(value)) for key, value in headers]


def _header_all(headers, name: str) -> list[str]:
    wanted = name.lower()
    return [value for key, value in _header_items(headers) if key.lower() == wanted]


def _header(headers, name: str) -> str | None:
    values = _header_all(headers, name)
    return values[0] if values else None


class _DeadlineGuard:
    """Hard wall-clock stop for one request.

    ``sock.settimeout()`` is a per-recv *idle* timer, so a server that dribbles a byte at a
    time (TLS records, header bytes, chunk fragments) resets it forever and holds the fetch
    open long past the advertised deadline — the between-calls ``_remaining()`` checks never
    get a turn. A background timer shuts the live socket down instead, which makes the wall
    deadline real for every blocking step: CONNECT, handshake, headers and body.
    """

    def __init__(self, deadline: float) -> None:
        self._lock = threading.Lock()
        self._sock = None
        self.expired = False
        self._timer = threading.Timer(max(0.0, deadline - time.monotonic()), self._fire)
        self._timer.daemon = True
        self._timer.start()

    def arm(self, sock) -> None:
        """Track the socket the request is currently blocking on (re-armed after TLS wrap)."""
        with self._lock:
            expired = self.expired
            self._sock = sock
        if expired:
            self._drop(sock)
            raise TimeoutError("subscription fetch deadline exceeded")

    def check(self) -> None:
        if self.expired:
            raise TimeoutError("subscription fetch deadline exceeded")

    def cancel(self) -> None:
        self._timer.cancel()

    def _fire(self) -> None:
        with self._lock:
            self.expired = True
            sock = self._sock
        self._drop(sock)

    @staticmethod
    def _drop(sock) -> None:
        if sock is None:
            return
        try:                      # shutdown (not just close) is what wakes a blocked recv
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


def _proxy_connect(sock: socket.socket, pinned_ip: str, port: int, deadline: float) -> None:
    rendered_ip = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
    target = f"{rendered_ip}:{port}"
    request = (f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
               "Proxy-Connection: keep-alive\r\n\r\n").encode("ascii")
    sock.settimeout(_remaining(deadline))
    sock.sendall(request)
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        sock.settimeout(_remaining(deadline))
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("subscription proxy closed CONNECT")
        buf.extend(chunk)
        if len(buf) > 64 * 1024:
            raise ValueError("subscription proxy response headers too large")
    status_line = bytes(buf).split(b"\r\n", 1)[0].decode("ascii", "replace")
    fields = status_line.split(" ", 2)
    if len(fields) < 2 or fields[1] != "200":
        raise ConnectionError(f"subscription proxy CONNECT failed: {status_line}")


def _request_once(parts, pinned_ip: str, headers: dict, proxy: str | None,
                  deadline: float) -> tuple[int, list[tuple[str, str]], bytes]:
    """One pinned GET. Host and TLS SNI remain the original provider hostname."""
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").encode("idna").decode("ascii")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("invalid subscription URL port") from exc
    host_header = _authority(host, port, scheme)
    proxy_parts = urllib.parse.urlsplit(proxy) if proxy else None
    if proxy_parts and (proxy_parts.scheme != "http" or not proxy_parts.hostname):
        raise ValueError("subscription proxy must be an http URL")
    connect_host = proxy_parts.hostname if proxy_parts else pinned_ip
    connect_port = (proxy_parts.port or 80) if proxy_parts else port
    guard = _DeadlineGuard(deadline)
    sock = None
    try:
        sock = socket.create_connection((connect_host, connect_port), timeout=_remaining(deadline))
        guard.arm(sock)
        if proxy_parts:
            # CONNECT to the pinned address for both HTTP and HTTPS. Sending an absolute HTTP
            # URL with the original Host would let a permissive proxy resolve it again.
            _proxy_connect(sock, pinned_ip, port, deadline)
        if scheme == "https":
            sock.settimeout(_remaining(deadline))
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            guard.arm(sock)
        path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
        target = path
        clean = {str(k): str(v) for k, v in headers.items()
                 if str(k).lower() not in {"host", "connection", "content-length"}}
        clean["Host"] = host_header
        clean["Connection"] = "close"
        lines = [f"GET {target} HTTP/1.1", *(f"{k}: {v}" for k, v in clean.items()), "", ""]
        sock.settimeout(_remaining(deadline))
        sock.sendall("\r\n".join(lines).encode("iso-8859-1"))
        response = http.client.HTTPResponse(sock)
        sock.settimeout(_remaining(deadline))
        response.begin()
        # keep the raw pair list: dict() would drop all but one Set-Cookie / Subscription-Userinfo
        response_headers = response.getheaders()
        declared_length = None
        content_length = _header(response_headers, "content-length")
        if content_length:
            try:
                declared_length = int(content_length)
            except ValueError:
                declared_length = None
            if declared_length is not None and declared_length > MAX_BYTES:
                raise ValueError(f"subscription body exceeds the {MAX_BYTES // 1024} KiB cap")
        body = bytearray()
        while True:
            _remaining(deadline)
            chunk = response.read(min(64 * 1024, MAX_BYTES + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > MAX_BYTES:
                raise ValueError(f"subscription body exceeds the {MAX_BYTES // 1024} KiB cap")
        guard.check()
        # http.client returns b"" on a premature close instead of raising, so a feed cut short
        # mid-body would parse as a valid *prefix* — and reconcile would then delete every node
        # that fell off the end. Insist the body is exactly as long as the server declared.
        if declared_length is not None and len(body) != declared_length:
            raise ValueError("subscription response was truncated "
                             f"({len(body)} of {declared_length} declared bytes)")
        if getattr(response, "length", None):
            raise ValueError("subscription response was truncated (connection closed early)")
        return response.status, response_headers, bytes(body)
    except http.client.HTTPException as exc:
        if guard.expired:
            raise TimeoutError("subscription fetch deadline exceeded") from exc
        # a missing terminal chunk surfaces here (IncompleteRead) — a failed fetch, not a body
        raise ValueError(f"subscription response was malformed or truncated: {exc}") from exc
    except OSError as exc:
        if guard.expired:
            raise TimeoutError("subscription fetch deadline exceeded") from exc
        raise
    finally:
        guard.cancel()
        if sock is not None:
            sock.close()


def _charset(headers) -> str:
    """The declared charset, but only if we recognise it. The label is feed-controlled and
    ``bytes.decode`` reaches every registered codec by name — ``idna``/``punycode`` raise
    UnicodeError (not LookupError) on ordinary input, and ``zip``/``bz2`` aren't text at all."""
    content_type = _header(headers, "content-type") or ""
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset" and value:
            label = value.strip().strip('"\'').strip().lower()
            return label if label in ALLOWED_CHARSETS else "utf-8"
    return "utf-8"


def _cross_origin_headers(headers: dict) -> dict:
    """Headers safe to replay to a *different* origin after a redirect — the injected ones
    (x-hwid, provider tokens, Authorization) must not follow the hop."""
    return {key: value for key, value in headers.items()
            if str(key).lower() in SAFE_CROSS_ORIGIN_HEADERS}


def _credentials_travel(origin: tuple, hop: tuple) -> bool:
    """Whether the injected headers may follow this redirect. Same origin, yes; a different
    host, never. The one scheme change allowed is a same-host http→https *upgrade* on default
    ports, which providers routinely redirect to and which cannot leak anything the first hop
    did not already send in the clear."""
    scheme, host, port = origin
    hop_scheme, hop_host, hop_port = hop
    if host != hop_host:
        return False
    if scheme == hop_scheme:
        return port == hop_port
    return (scheme, port, hop_scheme, hop_port) == ("http", 80, "https", 443)


def _http_get(url: str, headers: dict, proxy: str | None,
              timeout: float) -> tuple[str, list[tuple[str, str]]]:
    """GET with one resolve/connect/read deadline and DNS pinning repeated on redirects."""
    deadline = time.monotonic() + timeout
    current = url
    origin = None
    cookies: dict[str, dict[str, str]] = {}
    for hop in range(MAX_REDIRECTS + 1):
        parts = urllib.parse.urlsplit(current)
        if parts.scheme.lower() not in ALLOWED_SCHEMES or not parts.hostname:
            raise ValueError("subscription URL must use http/https and include a host")
        try:
            port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
        except ValueError as exc:
            raise ValueError("invalid subscription URL port") from exc
        pinned_ip = _resolve_public(parts.hostname, port, deadline)
        hop_origin = (parts.scheme.lower(), parts.hostname.lower(), port)
        if origin is None:
            origin = hop_origin
        # the cookie jar is host-scoped already; the injected headers were not (they were
        # replayed verbatim onto whatever host the feed redirected us to).
        request_headers = (dict(headers) if _credentials_travel(origin, hop_origin)
                           else _cross_origin_headers(headers))
        request_headers["Host"] = _authority(parts.hostname, port, parts.scheme.lower())
        jar = cookies.get(parts.hostname.lower())
        if jar:
            request_headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in jar.items())
        status, response_headers, raw = _request_once(
            parts, pinned_ip, request_headers, proxy, deadline)
        for set_cookie in _header_all(response_headers, "set-cookie"):
            parsed_cookie = http.cookies.SimpleCookie()
            parsed_cookie.load(set_cookie)
            cookies.setdefault(parts.hostname.lower(), {}).update(
                {key: morsel.value for key, morsel in parsed_cookie.items()})
        if status in {301, 302, 303, 307, 308}:
            location = _header(response_headers, "location")
            if not location:
                raise ValueError("subscription redirect missing Location")
            if hop >= MAX_REDIRECTS:
                raise ValueError("too many subscription redirects")
            current = urllib.parse.urljoin(current, location)
            continue
        if status < 200 or status >= 300:
            raise ValueError(f"subscription endpoint returned HTTP {status}")
        try:
            body = raw.decode(_charset(response_headers), "replace")
        except (LookupError, ValueError):   # ValueError covers UnicodeError from odd codecs
            body = raw.decode("utf-8", "replace")
        return body, response_headers
    raise ValueError("too many subscription redirects")


def fetch(url: str, injection: dict, tokens: dict, *,
          proxy: str | None) -> tuple[str, str, list[tuple[str, str]]]:
    """GET one provider feed, direct or through the local Xray HTTP proxy."""
    req = build_request(url, injection, tokens)
    parts = urllib.parse.urlsplit(req.url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError(f"unsupported URL scheme '{parts.scheme or '(none)'}': only http/https allowed")
    path = "tunnel" if proxy else "direct"
    body, response_headers = _http_get(req.url, req.headers, proxy, 20.0)
    return body, path, response_headers
