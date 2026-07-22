import concurrent.futures
import http.client
import http.cookies
import ipaddress
import socket
import ssl
import time
import urllib.parse

from pi_gw_panel.subs.inject import build_request

ALLOWED_SCHEMES = ("http", "https")
ALLOW_LOOPBACK = False   # test seam: integration tests fetch from a local stub server
MAX_BYTES = 1024 * 1024
MAX_REDIRECTS = 5
_DNS_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4,
                                                   thread_name_prefix="sub-dns")


def _ip_blocked(addr) -> bool:
    return (addr.is_loopback or addr.is_private or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("subscription fetch deadline exceeded")
    return remaining


def _resolve_public(host: str, port: int, deadline: float) -> str:
    """Resolve one hop once, reject mixed public/private answers, and return the pinned IP."""
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
    if not ALLOW_LOOPBACK and any(_ip_blocked(address) for address in addresses):
        raise ValueError("subscription URL resolves to a non-public (internal) address")
    return str(addresses[0])


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


def _header(headers: dict, name: str) -> str | None:
    wanted = name.lower()
    return next((str(value) for key, value in headers.items() if key.lower() == wanted), None)


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
                  deadline: float) -> tuple[int, dict, bytes]:
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
    sock = socket.create_connection((connect_host, connect_port), timeout=_remaining(deadline))
    try:
        if proxy_parts:
            # CONNECT to the pinned address for both HTTP and HTTPS. Sending an absolute HTTP
            # URL with the original Host would let a permissive proxy resolve it again.
            _proxy_connect(sock, pinned_ip, port, deadline)
        if scheme == "https":
            sock.settimeout(_remaining(deadline))
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
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
        response_headers = dict(response.getheaders())
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
            sock.settimeout(_remaining(deadline))
            chunk = response.read(min(64 * 1024, MAX_BYTES + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > MAX_BYTES:
                raise ValueError(f"subscription body exceeds the {MAX_BYTES // 1024} KiB cap")
        return response.status, response_headers, bytes(body)
    finally:
        sock.close()


def _charset(headers: dict) -> str:
    content_type = _header(headers, "content-type") or ""
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset" and value:
            return value.strip('"\'')
    return "utf-8"


def _http_get(url: str, headers: dict, proxy: str | None, timeout: float) -> tuple[str, dict]:
    """GET with one resolve/connect/read deadline and DNS pinning repeated on redirects."""
    deadline = time.monotonic() + timeout
    current = url
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
        request_headers = dict(headers)
        request_headers["Host"] = _authority(parts.hostname, port, parts.scheme.lower())
        jar = cookies.get(parts.hostname.lower())
        if jar:
            request_headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in jar.items())
        status, response_headers, raw = _request_once(
            parts, pinned_ip, request_headers, proxy, deadline)
        set_cookie = _header(response_headers, "set-cookie")
        if set_cookie:
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
        except LookupError:
            body = raw.decode("utf-8", "replace")
        return body, response_headers
    raise ValueError("too many subscription redirects")


def fetch(url: str, injection: dict, tokens: dict, *, proxy: str | None) -> tuple[str, str, dict]:
    """GET one provider feed, direct or through the local Xray HTTP proxy."""
    req = build_request(url, injection, tokens)
    parts = urllib.parse.urlsplit(req.url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError(f"unsupported URL scheme '{parts.scheme or '(none)'}': only http/https allowed")
    path = "tunnel" if proxy else "direct"
    body, response_headers = _http_get(req.url, req.headers, proxy, 20.0)
    return body, path, response_headers
