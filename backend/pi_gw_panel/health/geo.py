"""Egress IP → ISO-3166 country code, for the flag shown next to each egress IP.

Offline: reads the bundled DB-IP Lite country mmdb (`maxminddb`). Resolution happens on read
(the egress IPs are already stored in node_health) and is cached in memory — egress IPs are few
and stable. Everything degrades to None gracefully: no DB (dev/CI), a private/unknown IP, or any
reader error → no country → the UI shows the IP without a flag."""
import logging
import threading

_log = logging.getLogger("pi_gw_panel")

_DEFAULT_DB = "/usr/local/share/dbip-country-lite.mmdb"
_db_path = _DEFAULT_DB
_reader = None              # cached maxminddb reader, or False once we've failed to open it
_cache: dict[str, str | None] = {}
# country_code runs on request threads while configure/clear_cache can reset the reader/cache
# concurrently — serialize reader open + cache access so we can't leak readers or read a
# half-swapped reader mid-flip.
_lock = threading.Lock()


def configure(db_path: str) -> None:
    """Point the resolver at a db path (from settings; called once at startup). Resets the cache."""
    global _db_path
    _db_path = db_path or _DEFAULT_DB
    clear_cache()


def lookup_cc(reader, ip: str) -> str | None:
    """Pure: the 2-letter country for `ip` per an open mmdb `reader`, or None. No reader, a
    private/unrecorded IP, a record without a country, or any reader error → None."""
    if reader is None or not ip:
        return None
    try:
        rec = reader.get(ip)
    except Exception:           # invalid IP string / reader error → unknown, never raise
        return None
    if not rec:
        return None
    cc = (rec.get("country") or {}).get("iso_code")
    return cc or None


def _get_reader():
    """Lazily open + cache the mmdb reader; None if the file is absent or unreadable. Caller holds _lock."""
    global _reader
    if _reader is None:
        try:
            import maxminddb
            _reader = maxminddb.open_database(_db_path)
        except Exception as exc:        # missing file (dev/CI), bad db, maxminddb absent
            _log.info("geoip db unavailable (%s) — egress flags disabled", exc)
            _reader = False
    return _reader or None


def country_code(ip: str) -> str | None:
    """ISO-2 country for `ip`, or None. Caches by IP (egress IPs are few + stable)."""
    if not ip:
        return None
    with _lock:
        if ip in _cache:
            return _cache[ip]
        cc = lookup_cc(_get_reader(), ip)
        _cache[ip] = cc
        return cc


def clear_cache() -> None:
    """Reset the reader + result cache (tests; or after a db refresh)."""
    global _reader
    with _lock:
        _reader = None
        _cache.clear()
