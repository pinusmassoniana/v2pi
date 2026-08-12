import copy
import hmac
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Request, HTTPException, Header
from pi_gw_panel.api.schemas import (
    LoginIn, SetupIn, PasswordChangeIn, NodeIn, NodeOut, NodeUpdate, StatusOut,
    SubscriptionIn, SubscriptionPatch, SubscriptionOut, RefreshAllOut,
    PreviewIn, PreviewOut, PreviewNodesOut, PreviewNodeOut, ReorderIn, ConnectBestIn,
    ImportNodesIn, ImportNodesOut, DetachIn, NodeValidateIn, NodeValidateOut,
    SettingsOut, SettingsIn, DiagnosticsOut,
    ProfileIn, ProfileUpdate, ProfileOut, DefaultProfileIn,
    ProfileValidateOut, ProfilePresetInfo,
    RoutingIn, RoutingOut, RoutingRuleOut, RoutingValidateOut, PresetInfo, NodeHealthOut,
    NetworkOut, NetworkIn, NetworkSegmentOut, NetworkStatusOut, RouterRecOut,
    ConnEventOut, TrafficHistoryOut,
    TokenCreateIn, TokenOut, TokenCreatedOut, AuditEntryOut,
    RwOut, RwIn, RwClientIn, RwClientPatch, RwClientOut, RwLinkOut, RwConfigOut, RwShortIdOut,
)
from pi_gw_panel.api.deps import get_state, require_auth, require_csrf
from pi_gw_panel.auth.auth import (
    SESSION_AUTHED, SESSION_CSRF, SESSION_EPOCH, SESSION_LASTSEEN, new_csrf_token)
from pi_gw_panel.auth import service as auth_service
from pi_gw_panel.auth import tokens
from pi_gw_panel.models import Node, Subscription, TuningProfile, RoutingRule, NodeHealth
from pi_gw_panel.controller import (
    ApplyResult, apply_node, apply_net, reapply_active_node, build_node_config, apply_lock,
    stop_net, sync_net, restore_backup,
    RW_PENDING_KEY, rw_clear_reconcile_pending, rw_reconcile_is_pending)
from pi_gw_panel.net_control import netcheck, events as conn_events
from pi_gw_panel.net_control.plan import NetPlan, net24
from pi_gw_panel import rw_inbound as rw_mod
from pi_gw_panel.stats.history import bounded_interval_ms
from pi_gw_panel.health import probe, geo
from pi_gw_panel.health.selection import best_node
from pi_gw_panel.health.snapshot import health_status
from pi_gw_panel import backup as backup_mod
from pi_gw_panel import logs as logs_mod
from pi_gw_panel.xray_config.routing import PRESETS, preset_rules, validate_routing
from pi_gw_panel.xray_config.tuning import resolve_profile, validate_profile, PROFILE_PRESETS
from pi_gw_panel.xray_config.builder import (build_config, rw_inbound_block, rw_grants,
                                             rw_lan_outbound, rw_lan_rule, RW_TAG,
                                             DIRECT_LAN_TAG)
from pi_gw_panel.xray_config.validate import ConfigManager, config_digest, validate_config
from pi_gw_panel.config import (NET_CROSS_FIELD_KEYS, SETTINGS_DEFAULTS, check_change_safe,
                                safe_int, validate_net_settings, validate_setting_values)
from pi_gw_panel.subs.inject import build_request, default_injection, host_tokens
from pi_gw_panel.subs import service
from pi_gw_panel.subs.fetcher import assert_public_url, fetch
from pi_gw_panel.subs.parsers import clamp_node_fields
from pi_gw_panel.subs.parsers.dispatch import parse_subscription, detect

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _health_out(h) -> NodeHealthOut:
    """Serialize a NodeHealth row + the derived egress country (flag in the UI)."""
    return NodeHealthOut(**vars(h), egress_cc=geo.country_code(h.egress_ip),
                         egress_cc6=geo.country_code(h.egress_ip6))


def _node_out(n: Node) -> NodeOut:
    return NodeOut(id=n.id, name=n.name, address=n.address, port=n.port, uuid=n.uuid,
                   transport=n.transport, network=n.network, security=n.security,
                   sni=n.sni, public_key=n.public_key, short_id=n.short_id,
                   fingerprint=n.fingerprint, path=n.path, host=n.host, mode=n.mode, alpn=n.alpn,
                   note=n.note, subscription_id=n.subscription_id, stale=n.stale,
                   tuning_profile_id=n.tuning_profile_id)


def _clamp_interval(sec: int) -> int:
    """R3: 0 = manual-only; otherwise floor the auto-update interval at 60s so a typo can't
    hammer the provider every scheduler tick."""
    return 0 if sec <= 0 else max(60, sec)


def _check_sub_url(url: str) -> None:
    """Run the fetcher's own scheme/SSRF check when the URL is saved, so a typo or an internal
    address is reported to the operator right there instead of silently failing on the first
    scheduled refresh (the fetch still re-pins and re-checks at request time)."""
    try:
        assert_public_url(url)
    except (ValueError, TimeoutError) as exc:
        raise HTTPException(status_code=422, detail=f"url: {exc}")


def _check_profile_id(store, profile_id: int | None) -> None:
    """Reject a reference to a tuning profile that doesn't exist, with 422 rather than the 500 the
    enforced foreign key would otherwise produce. None means "no profile" (inherit the default)
    and is always fine. Shared by every route that accepts a profile id — node create/edit,
    subscription create/edit and the node pre-flight — so they answer alike."""
    if profile_id is not None and store.get_profile(profile_id) is None:
        raise HTTPException(status_code=422, detail="tuning profile not found")


_PROFILE_FIELDS = ("id", "name", "fingerprint", "frag_enabled", "frag_packets",
                   "frag_length", "frag_interval", "mux_enabled", "doh_enabled",
                   "doh_url", "quic", "noise_enabled", "noises", "xhttp_padding",
                   "xmux_max_concurrency", "xmux_max_connections", "mux_concurrency",
                   "xudp_proxy_udp443", "alpn", "tls_min", "tls_max")


def _profile_out(p: TuningProfile, default_id: int | None,
                 active_pid: int | None = None, node_count: int = 0) -> ProfileOut:
    return ProfileOut(is_default=(default_id is not None and default_id == p.id),
                      is_active=(active_pid is not None and active_pid == p.id),
                      node_count=node_count,
                      **{f: getattr(p, f) for f in _PROFILE_FIELDS})


def _active_resolved_pid(store) -> int | None:
    """The profile id that governs the live tunnel right now — the active node's assigned
    profile, or the default it inherits. None when no node is active."""
    aid = store.get_setting("active_node_id")
    if not aid:
        return None
    node = store.get_node(int(aid))
    if node is None:
        return None
    p = resolve_profile(store, node)
    return p.id if p else None


def _reapply_or_502(state) -> None:
    res = reapply_active_node(state)
    if res is not None and not res.ok:
        raise HTTPException(status_code=502, detail=res.error)


def _rule_out(r) -> RoutingRuleOut:
    return RoutingRuleOut(id=r.id or 0, position=r.position, type=r.type, value=r.value,
                          action=r.action, enabled=getattr(r, "enabled", True),
                          label=getattr(r, "label", ""))


def _routing_out(state, rules=None) -> RoutingOut:
    rules = state.store.get_routing() if rules is None else rules
    return RoutingOut(
        rules=[_rule_out(r) for r in rules],
        default_action=state.store.get_setting("routing_default_action") or "proxy",
        domain_strategy=state.store.get_setting("routing_domain_strategy") or "IPIfNonMatch")


def _sub_out(state, sub: Subscription, node_count: int | None = None) -> SubscriptionOut:
    if node_count is None:
        node_count = len(state.store.list_nodes_for_sub(sub.id))
    return SubscriptionOut(
        id=sub.id, name=sub.name, url=sub.url, injection=sub.injection,
        interval_sec=sub.interval_sec, enabled=sub.enabled,
        default_profile_id=sub.default_profile_id, last_fetched=sub.last_fetched,
        last_status=sub.last_status, last_path=sub.last_path, last_error=sub.last_error,
        up_bytes=sub.up_bytes, down_bytes=sub.down_bytes, total_bytes=sub.total_bytes,
        expire_at=sub.expire_at, node_count=node_count)


def _pick_best_node(store, subscription_id):
    """N9: the healthiest non-stale node in a scope (a subscription id, or None for manual).
    Shares the failover scorer (real > http > tcp, lowest latency); blind-picks the first when
    no node has health yet."""
    nodes = [n for n in store.list_nodes() if n.subscription_id == subscription_id]
    health = {h.node_id: h for h in store.list_health()}
    return best_node(nodes, health)


def _settings_out(state) -> SettingsOut:
    m = state.store.get_settings_map()   # one query instead of ~15 (OB6)

    def val(key: str) -> str:
        return m.get(key) or SETTINGS_DEFAULTS[key]

    def num(key: str) -> int:
        """A stored value that isn't a number falls back to the default instead of raising.

        Values are range-checked on every write path now, so a bad one means a hand-edited DB or
        a document restored by an older build. int()-ing it here used to make this endpoint —
        and with it the whole Settings screen, the one place the operator could fix it — 500
        for good; the panel has to stay reachable enough to repair itself."""
        return safe_int(val(key), int(SETTINGS_DEFAULTS[key]), key)
    return SettingsOut(
        tunneled_fetch=val("tunneled_fetch") == "1",
        subs_auto_switch=val("subs_auto_switch") == "1",
        routing_default_action=val("routing_default_action"),
        health_enabled=val("health_enabled") == "1",
        health_sweep_enabled=val("health_sweep_enabled") == "1",
        health_interval=num("health_interval"),
        health_active_interval=num("health_active_interval"),
        health_hysteresis=num("health_hysteresis"),
        health_probe_url=val("health_probe_url"),
        failover_enabled=val("failover_enabled") == "1",
        failover_cooldown=num("failover_cooldown"),
        stats_enabled=val("stats_enabled") == "1",
        stats_api_port=num("stats_api_port"),
        traffic_sample_ms=num("traffic_sample_ms"),
        dns_intercept=val("dns_intercept") == "1",
        session_timeout_min=num("session_timeout_min"),
        auto_backup_enabled=val("auto_backup_enabled") == "1")


_NET_EDITABLE = ("segment_iface", "segment_ip", "segment_ip6",
                 "dhcp_start", "dhcp_end", "dhcp_lease", "client_dns", "client_dns6")
# Everything but segment_ip6: for these, "" is not a value the operator can mean. segment_ip6 is
# genuinely clearable (empty = no static prefix / v6 off), so an empty one is kept, not rejected.
_NET_REQUIRED = tuple(f for f in _NET_EDITABLE if f != "segment_ip6")


def _validate_net_fields(data: dict, state) -> None:
    """Reject any segment/DHCP/DNS value that isn't well-formed BEFORE it reaches set_setting and,
    from there, the nft/dnsmasq render (config-injection + broken-segment guard). Raises 422.

    The rules themselves live in `config.validate_net_settings`, shared with restore: these
    values are equally settable from a backup document, and the local copy this used to keep
    accepted a trailing newline (`re.match(r'…$', 'eth0\\n')` succeeds) that the shared
    `fullmatch` does not. Normalized values are merged back so what is stored is what was checked.

    Strip first, then reject. `validate_net_settings` reads an all-whitespace value as absent and
    returns nothing for it, so `" "` used to skip validation entirely and be stored verbatim —
    and `net_control.plan` treats `" "` as a set value, so that one space silently displaced the
    working default. Schema `min_length=1` doesn't catch it either: a space is one character."""
    for field in _NET_EDITABLE:
        value = data.get(field)
        if isinstance(value, str):
            data[field] = value.strip()
            if not data[field] and field in _NET_REQUIRED:
                raise HTTPException(status_code=422, detail=f"{field}: must not be blank")
    try:
        data.update(validate_net_settings(data))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # ...and then the checks no single field can make. Every rule above sees one value in
    # isolation, so the panel used to accept `segment_iface == mgmt_iface` — a kill-switch drop and
    # a tproxy redirect installed on the management leg — and hand the operator's own access away
    # on request. `check_change_safe` refuses that class outright, because the screen that would
    # take the change back is on the far side of the interface the change breaks.
    #
    # Here, and not deeper: this runs BEFORE the transaction opens and before any host command, so
    # a refusal leaves nothing persisted and nothing applied. It is given the EFFECTIVE values (the
    # request merged over what is stored) — a partial edit collides just as well with a field it
    # does not mention, and the ONLY thing that matters is the config that would result.
    plan = NetPlan.from_store(state.store, state.settings)
    problems = check_change_safe(
        {f: (data.get(f) or getattr(plan, f, "")) for f in NET_CROSS_FIELD_KEYS}, state.settings)
    if problems:
        raise HTTPException(status_code=422, detail="; ".join(problems))


def _network_out(state) -> NetworkOut:
    store, settings = state.store, state.settings
    def ov(key: str) -> str:                       # editable field: DB override or config
        return store.get_setting(key) or getattr(settings, key)
    # C1: only the real Pi backend probes the uplink (a live socket); dev/CI = unknown.
    uplink_check = netcheck.uplink_up if type(state.net).__name__ == "LinuxBackend" else (lambda: None)
    kill_switch = (store.get_setting("kill_switch_enabled") or "1") == "1"
    lan_access = (store.get_setting("lan_access_enabled") or ("1" if settings.lan_access else "0")) == "1"
    ipv6_enabled = (store.get_setting("ipv6_enabled") or "0") == "1"
    st = netcheck.network_status(store, settings, uplink_check=uplink_check)
    # Host enforcement is transient fact, not intent. It remains unknown until a backend
    # operation returns a verified result, and becomes nullable/error after a failed mutation.
    st["wan_blocked"] = getattr(state.net, "wan_blocked", None)
    st["enforcement_status"] = getattr(state.net, "enforcement_status", "unknown")
    st["enforcement_error"] = getattr(state.net, "enforcement_error", "")
    if ipv6_enabled and type(state.net).__name__ == "LinuxBackend":
        # DHCPv6-PD 'auto': observe the host-delegated segment prefix (reads /proc/net/if_inet6).
        v6 = (ov("segment_ip6") or "").strip().lower()
        if v6 == "auto":
            st["ipv6_prefix"] = netcheck.segment_prefix6(ov("segment_iface"))
        st["uplink6"] = netcheck.uplink_up("2606:4700:4700::1111")   # G: v6 uplink reachability
        st["foreign_ra"] = netcheck.foreign_ra(ov("segment_iface"))  # Phase C: rogue-RA leak
        st["ipv6_prefix_source"] = "pd" if v6 == "auto" else "static" if v6 else "ula"
    return NetworkOut(
        segment=NetworkSegmentOut(
            iface=ov("segment_iface"), ip=ov("segment_ip"), ip6=ov("segment_ip6"),
            dhcp_start=ov("dhcp_start"), dhcp_end=ov("dhcp_end"),
            dhcp_lease=ov("dhcp_lease"), client_dns=ov("client_dns"),
            client_dns6=ov("client_dns6")),
        kill_switch_enabled=kill_switch,
        lan_access_enabled=lan_access,
        ipv6_enabled=ipv6_enabled,
        status=NetworkStatusOut(**st),
        recommendations=[RouterRecOut(**r) for r in netcheck.router_recommendations(
            NetPlan.from_store(store, settings), ipv6_enabled, ov("segment_ip6"))],
        events=[ConnEventOut(**e) for e in conn_events.recent(store)])


_START_TIME = time.time()


def _open_session(request: Request) -> None:
    store = get_state(request).store
    request.session[SESSION_AUTHED] = True
    request.session[SESSION_EPOCH] = auth_service.session_epoch(store)
    request.session[SESSION_LASTSEEN] = int(time.time())
    request.session[SESSION_CSRF] = new_csrf_token()


# --- auth ---
@router.get("/setup")
def setup_status(request: Request) -> dict:
    """Open: whether first-run credential setup is still needed."""
    state = get_state(request)
    return {"needs_setup": auth_service.needs_setup(state.store),
            "bootstrap_required": (not state.settings.loopback_bind
                                   and os.path.isfile(state.settings.bootstrap_token_path))}


@router.post("/setup")
def setup_create(body: SetupIn, request: Request,
                 x_bootstrap_token: str | None = Header(default=None)) -> dict:
    """Open by necessity (no credential exists yet) — creates the one-and-only
    credential and opens a session. 409 once configured (no re-setup via this route)."""
    state = get_state(request)
    token_path = state.settings.bootstrap_token_path
    expected = ""
    if not state.settings.loopback_bind:
        try:
            with open(token_path) as handle:
                expected = handle.read().strip()
        except OSError:
            pass
    if expected:
        # Compare as bytes: Starlette decodes headers as latin-1 and compare_digest raises
        # TypeError on a non-ASCII str, which would turn a garbage bootstrap header into a 500
        # on this unauthenticated route instead of the 403 it deserves.
        if not x_bootstrap_token or not hmac.compare_digest(
                expected.encode("utf-8"), x_bootstrap_token.encode("utf-8")):
            raise HTTPException(status_code=403, detail="bad bootstrap token")
    elif not state.settings.loopback_bind:
        raise HTTPException(status_code=503, detail="bootstrap token unavailable")
    try:
        auth_service.create_credential(state.store, body.username, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="already configured") from exc
    if os.path.isfile(token_path):
        try:
            os.unlink(token_path)
        except FileNotFoundError:
            pass
    _open_session(request)
    return {"ok": True}


_LOGIN_GUARD_MAX = 1000    # hard cap on tracked IP buckets (memory bound)
_login_guard_lock = threading.Lock()   # sync handlers run in a threadpool → serialize guard R-M-W
_login_kdf_slots = threading.BoundedSemaphore(2)


def _evict_login_guard(guards: dict, now: float) -> bool:
    """Free at least one bucket slot. True when a slot is available afterwards.

    Preference order matters. Buckets carrying no state (not locked out, not mid-count) are
    dropped first — an attacker must not be able to clear their own failure count by flooding
    the table from other addresses. Only when every bucket carries state do we fall back to
    evicting the least-recently-seen one: buckets sitting at count 1..4 have `until == 0.0` and
    were never eligible for the state-free sweep, so a table filled with them used to wedge at
    the cap and blanket-429 every untracked client until the process restarted. A bucket with a
    request in flight is never evicted (its handler still holds the reference)."""
    idle = [key for key, g in guards.items()
            if g["until"] <= now and g["count"] == 0 and g.get("in_flight", 0) == 0]
    if idle:
        for key in idle:
            del guards[key]
        return True
    stale = [(g.get("seen", 0.0), key) for key, g in guards.items() if g.get("in_flight", 0) == 0]
    if not stale:
        return False
    del guards[min(stale)[1]]
    return True


@router.post("/login")
def login(body: LoginIn, request: Request) -> dict:
    state = get_state(request)
    guards = request.app.state.login_guard         # per-client-IP buckets (SS3 / audit B8)
    now = time.time()
    ip = request.client.host if request.client else "?"
    # Serialize the whole read-modify-write: concurrent requests from one IP would otherwise race
    # on guard["count"] (lost updates) and let an attacker exceed the 5-attempt lockout.
    with _login_guard_lock:
        # Only prune under memory pressure, and only when this client actually needs a new slot.
        if len(guards) >= _LOGIN_GUARD_MAX and ip not in guards:
            if not _evict_login_guard(guards, now):
                raise HTTPException(status_code=429, detail="too many clients — try again shortly")
        guard = guards.setdefault(ip, {"count": 0, "until": 0.0, "in_flight": 0, "seen": now})
        guard["seen"] = now                      # recency for the LRU fallback above
        locked = guard["until"] > now
        saturated = guard.get("in_flight", 0) >= 5
        if not locked and not saturated:
            guard["in_flight"] = guard.get("in_flight", 0) + 1
    if locked or saturated:
        raise HTTPException(status_code=429, detail="too many attempts — try again shortly")
    if not _login_kdf_slots.acquire(blocking=False):
        with _login_guard_lock:
            guard["in_flight"] -= 1
        raise HTTPException(status_code=429, detail="login verifier busy")
    try:
        valid = auth_service.verify_login(state.store, body.username, body.password)
    finally:
        _login_kdf_slots.release()
    with _login_guard_lock:
        guard["in_flight"] -= 1
        if valid:
            guard["count"] = 0
        else:
            guard["count"] += 1
            if guard["count"] >= 5:
                guard["until"] = now + state.settings.login_lockout_sec
                guard["count"] = 0
    if not valid:
        raise HTTPException(status_code=401, detail="bad credentials")
    _open_session(request)
    return {"ok": True}


@router.post("/logout")
def logout(request: Request, _: None = Depends(require_csrf)) -> dict:
    # require_csrf so a cross-site POST can't force-log-out the operator (consistent with every
    # other mutation); the frontend sends the CSRF header on logout.
    request.session.clear()
    return {"ok": True}


@router.post("/password")
def change_password(body: PasswordChangeIn, request: Request,
                    _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    state = get_state(request)
    username = state.store.get_setting("auth_username") or ""
    if not auth_service.verify_login(state.store, username, body.current_password):
        raise HTTPException(status_code=403, detail="current password incorrect")
    auth_service.set_password(state.store, body.new_password)   # rotates hash AND bumps the epoch
    # adopt the new epoch for THIS session so it stays valid while every other session is invalidated (SS3)
    request.session[SESSION_EPOCH] = auth_service.session_epoch(state.store)
    return {"ok": True}


@router.get("/csrf")
def csrf(request: Request, _: None = Depends(require_auth)) -> dict:
    return {"csrf": request.session.get(SESSION_CSRF)}


@router.get("/status", response_model=StatusOut)
def status(request: Request, _: None = Depends(require_auth)) -> StatusOut:
    state = get_state(request)
    st = state.supervisor.status()
    active = state.store.get_setting("active_node_id")
    since = state.store.get_setting("active_since")
    last_fo = state.store.get_setting("last_failover_at")
    prev = state.store.get_setting("prev_active_node_id")
    now = time.time()
    health = health_status(state.store, now=now)
    active_id = int(active) if active else None
    active_row = state.store.get_health(active_id) if active_id is not None else None
    health_enabled = (state.store.get_setting("health_enabled")
                      or SETTINGS_DEFAULTS["health_enabled"]) == "1"
    failover_enabled = (state.store.get_setting("failover_enabled")
                        or SETTINGS_DEFAULTS["failover_enabled"]) == "1"
    tunnel_online = bool(st["running"] and health["active_health_fresh"]
                         and active_row is not None and active_row.last_real_ok is True)
    failovers_24h = sum(
        event.get("kind") == "failover"
        and isinstance(event.get("ts"), (int, float))
        and now - 86400 <= event["ts"] <= now
        for event in conn_events.recent(state.store)
        if isinstance(event, dict)
    )
    # `running: true` says a process exists, not that it is serving the config on disk. The
    # comparison lives in netcheck so /api/ready decides on the same answer this reports, and it
    # costs one os.stat per poll: the digest of the file is memoized on (inode, mtime_ns, size),
    # so the read+parse+hash happens only after something actually rewrote it — this endpoint is
    # polled every 3s by every open dashboard tab (`subscribeStatus(3000)`), and re-hashing an
    # unchanged config that often is a cost with nothing to show for it.
    drift = netcheck.config_drift(state.supervisor)[0]
    return StatusOut(running=st["running"], pid=st["pid"],
                     active_node_id=active_id,  # "" (post-rollback) → None
                     xray_state=state.supervisor.state(),
                     active_since=int(since) if since else None,
                     last_failover_at=float(last_fo) if last_fo else None,
                     prev_active_node_id=int(prev) if prev else None,
                     # The honest answer to "would Rollback work": read-only, no side effects.
                     # A revocation intentionally invalidates the pairing, so prev_active_node_id
                     # can name a node while the rollback itself is refused.
                     rollback_available=ConfigManager(
                         state.settings, xray_bin=state.xray_bin).rollback_available(),
                     config_drift=drift,
                     server_now=now,   # D4: client offsets freshness/uptime by this
                     tunnel_online=tunnel_online,
                     active_health_fresh=health["active_health_fresh"],
                     failover_ready=(health_enabled and failover_enabled
                                     and health["eligible_standby_count"] > 0),
                     eligible_standby_count=health["eligible_standby_count"],
                     health_enabled=health_enabled,
                     failover_enabled=failover_enabled,
                     failovers_24h=failovers_24h)


@router.get("/traffic/history", response_model=TrafficHistoryOut)
def traffic_history(request: Request, window_sec: int = 3600, max_points: int = 600,
                    _: None = Depends(require_auth)) -> TrafficHistoryOut:
    """Seed the Dashboard graph with the recorded proxy throughput over the last
    `window_sec`, downsampled to at most `max_points` points (proxy outbound).

    Windows beyond the in-memory hour (N4) are served from the durable per-minute table
    (avg bit/s per minute), with the in-memory ring appended for the most recent stretch."""
    state = get_state(request)
    # clamp both ways: a huge window_sec would pull the entire per-minute history into memory, a
    # huge max_points sizes the downsample loop — cap them so one GET can't amplify into a DoS.
    window_sec = min(max(1, window_sec), 30 * 86400)     # <= 30 days
    max_points = min(max(1, max_points), 5000)
    interval = bounded_interval_ms(
        state.store.get_setting("traffic_sample_ms") or SETTINGS_DEFAULTS["traffic_sample_ms"])
    hist = getattr(state, "history", None)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    since_ms = now_ms - window_sec * 1000
    if window_sec <= 3600:
        if hist is None:
            return TrafficHistoryOut(samples=[], interval_ms=interval)
        series = hist.series(since_ms=since_ms, max_points=max(1, max_points))
        return TrafficHistoryOut(samples=[[s[0], s[1], s[2]] for s in series], interval_ms=interval)
    minutes = state.store.traffic_minutes(since_min=since_ms // 60000)
    # minute of bytes → avg bit/s, stamped at the minute's end (when those bytes finished)
    samples = [[(m["ts_min"] + 1) * 60000, round(m["up_bytes"] * 8 / 60), round(m["down_bytes"] * 8 / 60)]
               for m in minutes]
    last_ts = samples[-1][0] if samples else since_ms
    if hist is not None:                       # freshen the tail with the live ring
        samples += [[s[0], s[1], s[2]] for s in hist.series(since_ms=last_ts)]
    if len(samples) > max(1, max_points):      # stride down, always keeping the newest point
        step = len(samples) / max(1, max_points)
        out = [samples[int(i * step)] for i in range(max(1, max_points))]
        out[-1] = samples[-1]
        samples = out
    return TrafficHistoryOut(samples=samples, interval_ms=60000)


# --- nodes ---
@router.get("/nodes", response_model=list[NodeOut])
def list_nodes(request: Request, _: None = Depends(require_auth)) -> list[NodeOut]:
    state = get_state(request)
    return [_node_out(n) for n in state.store.list_nodes()]


@router.post("/nodes", response_model=NodeOut)
def add_node(body: NodeIn, request: Request,
             _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> NodeOut:
    state = get_state(request)
    # Node.__post_init__ normalizes transport↔network↔security↔flow, so an xhttp manual
    # node is built as xhttp (not silently tcp) and reality-without-key falls back to tls.
    node = Node(id=None, name=body.name, address=body.address, port=body.port,
                uuid=body.uuid, transport=body.transport, security=body.security,
                sni=body.sni, public_key=body.public_key, short_id=body.short_id,
                fingerprint=body.fingerprint, path=body.path, host=body.host,
                mode=body.mode, alpn=body.alpn, note=body.note)
    # `uq_nodes_identity` makes a re-add of the same server a constraint violation, not a crash:
    # report the conflict instead of letting sqlite3.IntegrityError surface as an opaque 500.
    try:
        nid = state.store.add_node(node)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="a node with this identity already exists") from exc
    saved = state.store.get_node(nid)
    if saved is None:  # unreachable: lastrowid is valid right after a successful insert
        raise HTTPException(status_code=500, detail="node vanished after insert")
    return _node_out(saved)


@router.patch("/nodes/{node_id}", response_model=NodeOut)
def update_node(node_id: int, body: NodeUpdate, request: Request,
                _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> NodeOut:
    state = get_state(request)
    node = state.store.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="node not found")
    if state.store.get_setting("active_node_id") == str(node_id):
        raise HTTPException(
            status_code=409, detail="disconnect the active node before editing it")
    # exclude_unset (not exclude_none) so an explicit `tuning_profile_id: null` clears
    # the assignment (→ inherit the global default) rather than being dropped.
    patch = body.model_dump(exclude_unset=True)
    if "tuning_profile_id" in patch:
        _check_profile_id(state.store, patch["tuning_profile_id"])
    for k, v in patch.items():
        setattr(node, k, v)
    # single source of truth: re-derive network/security/flow from the edited fields
    node.normalize()
    try:
        state.store.update_node(node)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="a node with this identity already exists") from exc
    return _node_out(state.store.get_node(node_id))


@router.delete("/nodes/{node_id}")
def delete_node(node_id: int, request: Request,
                _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    state = get_state(request)
    if state.store.get_node(node_id) is None:
        raise HTTPException(status_code=404, detail="node not found")
    if state.store.get_setting("active_node_id") == str(node_id):
        raise HTTPException(
            status_code=409, detail="disconnect the active node before deleting it")
    state.store.delete_node(node_id)
    return {"ok": True}


@router.post("/nodes/{node_id}/apply")
def apply(node_id: int, request: Request,
          _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    state = get_state(request)
    node = state.store.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="node not found")
    res = apply_node(node, state.settings, state.supervisor, state.net,
                     store=state.store, xray_bin=state.xray_bin)
    if not res.ok:
        raise HTTPException(status_code=502, detail=res.error)
    conn_events.record(state.store, "connect", f"connected to {node.name}")
    return {"ok": True}


@router.post("/nodes/{node_id}/disconnect")
def disconnect(node_id: int, request: Request,
               _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    """Disconnect `node_id` — which must be the currently active node — and clear the active
    selection. The net rules come down via stop_net: with the kill-switch ON the fail-closed
    leak-guard stays in place (so 'disconnect' doesn't leak client→WAN — A1); with it off, clients
    fall back to direct. xray is left running — the sidebar toggle is the only thing that stops
    xray-core.

    The path id is checked rather than ignored. There is only ever one active node, so the handler
    can only ever act on that one; accepting any id and reporting success let a stale UI (or an
    automation racing a failover) ask to disconnect node A, silently take down node B, and be told
    it worked. The id is read inside apply_lock so the answer can't be invalidated by a failover
    switching nodes between the check and the teardown."""
    state = get_state(request)
    with apply_lock:   # don't race a concurrent apply / failover tick (NR1)
        active = state.store.get_setting("active_node_id")
        if not active:
            raise HTTPException(status_code=409, detail="no node is connected")
        if active != str(node_id):
            raise HTTPException(
                status_code=409, detail=f"node {node_id} is not connected (active is {active})")
        result = stop_net(state.settings, state.net, state.store)
        if not result.ok:
            raise HTTPException(status_code=502, detail=result.error or "network stop failed")
        with state.store.transaction():
            prev = state.store.get_setting("active_node_id")
            state.store.set_setting("prev_active_node_id", prev or "")
            state.store.set_setting("active_node_id", "")
            state.store.set_setting("active_since", "")        # clear uptime anchor (P3)
    conn_events.record(state.store, "disconnect", "node disconnected")
    return {"ok": True}


@router.post("/xray/start")
def xray_start(request: Request,
               _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    """Start xray-core. If a node is active, bring its tunnel back up (config + net);
    otherwise just start the process — on the config already on disk, which is why that branch
    checks the file's remote-access grants against the store first (see `_rw_guard_start`)."""
    state = get_state(request)
    with apply_lock:   # reapply_active_node re-enters the lock (RLock); guards plain start()
        res = reapply_active_node(state)
        if res is None:
            _rw_guard_start(state)
            state.supervisor.start()
        elif not res.ok:
            raise HTTPException(status_code=502, detail=res.error)
    return {"ok": True}


@router.post("/xray/stop")
def xray_stop(request: Request,
              _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    """Stop xray-core. Net rules come down via stop_net: kill-switch ON keeps the
    fail-closed leak-guard (block client→WAN while the tunnel is down — A1); OFF tears down
    so clients fall back to direct rather than black-holing a dead tproxy port. The active
    selection is kept."""
    state = get_state(request)
    with apply_lock:
        # The stop is bounded now, so it can come back having NOT stopped anything (a child that
        # outlived SIGKILL). The net rules still come down — a process we cannot kill is exactly
        # when the fail-closed guard matters — but the answer says so rather than reporting a
        # stop that did not happen.
        stopped = state.supervisor.stop() is not False
        result = stop_net(state.settings, state.net, state.store)
        if not result.ok:
            raise HTTPException(status_code=502, detail=result.error or "network stop failed")
        if not stopped:
            raise HTTPException(status_code=502,
                                detail="xray did not stop (it survived SIGKILL)")
    conn_events.record(state.store, "xray-stop", "xray-core stopped")
    return {"ok": True}


def _bounded_stop(supervisor) -> tuple[bool, str]:
    """Stop xray for a RECOVERY path and report `(provably down, what to tell the operator)`.

    A bounded stop has two ways of not stopping anything and they mean the same thing to every
    caller here: False (the child outlived SIGKILL) and a raise out of terminate/wait/kill. Only
    the first was ever handled, so the second escaped the route — past the fail-closed network
    guard, which is precisely the thing an unkillable process calls for, and past the detail that
    names the survivor. Both now come back as a value, and neither can skip what follows.

    NOTHING is retried and nothing is started: the process is still up and still holding the
    port, so a second one would not be a recovery. The caller decides what to do with the fact;
    this only makes sure it HAS the fact.
    """
    try:
        if supervisor.stop() is False:
            return False, ("xray survived SIGKILL and is still running on the config it had "
                           "loaded")
    except Exception as exc:
        logger.error("a recovery stop raised: %s", exc)
        return False, (f"xray could not be stopped ({exc}) and may still be running on the "
                       "config it had loaded")
    return True, ""


@router.post("/rollback")
def rollback(request: Request,
             _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    """Promote the last-good config back to live, then make the runtime match it.

    The promoted config is by definition OLDER than the store, and remote-access credentials
    are the one thing where older means MORE access: a snapshot taken before a revocation still
    names the device that was cut off. A revocation drops the rollback target for exactly that
    reason, but the drop is a separate durable write that can fail — and on the paths that
    write no config at all it is the only guard there is. A failure there left a valid pairing
    behind, and this route would then reinstall the revoked credential and reload xray onto it.

    So the candidate is checked against the store BEFORE it is promoted, not after: it is
    rewritten to what the store actually grants and that is what gets installed, or the rollback
    is refused (409) with the live config never touched and the process still serving what it
    already had. Checking after the promotion was not a guard but a report — the 409 left the
    credential-bearing file installed as the live config, and the next start (the watchdog's
    included) came up on it. A rollback the operator asked for may cost them a config; it may
    never hand a revoked device its access back.
    """
    state = get_state(request)
    with apply_lock:
        mgr = ConfigManager(state.settings, xray_bin=state.xray_bin)
        candidate = mgr.rollback_target(log=True)
        ok = candidate is not None
        if ok:
            # Promotion happens in here, and only for a candidate proven serveable — BEFORE the
            # reload below, and before the stop/net branch too.
            _rw_guard_promotion(state, mgr, candidate)
            prev = state.store.get_setting("prev_active_node_id")
            # Both recovery stops below are BOUNDED now, so either can come back having stopped
            # nothing (a child that outlived SIGKILL). Their answers used to be discarded, which
            # on the no-previous-node branch meant the route went on to clear the active state
            # and answer {"ok": true} while xray was still running — a rollback reported as
            # complete with the old process still serving the config it had loaded.
            #
            # Both go through `_bounded_stop`, which also catches the OTHER way this stop fails.
            # `supervisor.stop()` shells out to terminate/wait/kill and can RAISE, and a raise
            # here used to leave the route entirely: on the reload-failed branch it skipped the
            # fail-closed network guard — the one thing a process we cannot kill actually calls
            # for — and on the no-previous-node branch it skipped the guard AND the detail naming
            # the survivor, turning both into a 500 that describes neither. A stop that could not
            # be confirmed is a fact to carry into the answer, whichever way it failed.
            stopped, stop_detail = True, ""
            stop_attempted = False
            if prev:
                if not state.supervisor.reload():
                    stopped, stop_detail = _bounded_stop(state.supervisor)
                    stop_attempted = True
                    guard = stop_net(state.settings, state.net, state.store)
                    detail = "rolled-back Xray did not become ready"
                    if not guard.ok:
                        detail += f"; fail-closed recovery failed: {guard.error}"
                    if stop_detail:
                        detail += f"; {stop_detail}"
                    raise HTTPException(status_code=502, detail=detail)
                net_result = apply_net(state.settings, state.net, state.store)
            else:
                stopped, stop_detail = _bounded_stop(state.supervisor)
                stop_attempted = True
                net_result = stop_net(state.settings, state.net, state.store)
            if not net_result.ok:
                # Ensure the least permissive available state even when restoring the previous
                # tunnel fails. The attempted guard outcome is reflected in enforcement_status.
                if not stop_attempted:
                    # Reached only from the branch above where the reload SUCCEEDED and it was
                    # `apply_net` that failed — so xray is up on the rolled-back config while the
                    # rules are about to be taken to their tunnel-down state. That end state,
                    # ruleset down and tunnel up, is the one this route may not answer with: the
                    # sibling branch has always stopped xray before installing the guard, and this
                    # one carried the same message while leaving the process running. Stop it
                    # first, then guard, exactly as the sibling does.
                    stopped, stop_detail = _bounded_stop(state.supervisor)
                guard = stop_net(state.settings, state.net, state.store)
                detail = net_result.error or "network rollback failed"
                if not guard.ok:
                    detail += f"; fail-closed recovery failed: {guard.error}"
                if stop_detail:
                    # A network failure does not make a surviving xray less interesting. This
                    # branch reported only the net error, so a simultaneous stop-and-net failure
                    # told the operator about the rules and not about the process still serving
                    # the config it had loaded — the more dangerous half of the pair.
                    detail += f"; {stop_detail}"
                raise HTTPException(status_code=502, detail=detail)
            if not stopped:
                # AFTER `stop_net` — the fail-closed guard is exactly what a process we cannot
                # kill calls for, so it is completed first — and BEFORE the store write, because
                # clearing the active selection would record a tunnel that is demonstrably still
                # up. The operator is told what survived rather than shown a success.
                raise HTTPException(status_code=502, detail=f"rolled back, but {stop_detail}")
            with state.store.transaction():
                state.store.set_setting("active_node_id", prev if prev else "")
                state.store.set_setting(
                    "active_since", str(int(datetime.now(timezone.utc).timestamp())) if prev else "")
    return {"ok": ok}


# --- tuning profiles ---
def _profiles_out(state) -> list[ProfileOut]:
    d = state.store.get_default_profile()
    did = d.id if d else None
    active_pid = _active_resolved_pid(state.store)
    counts: dict[int, int] = {}
    for n in state.store.list_nodes():
        if n.tuning_profile_id is not None:
            counts[n.tuning_profile_id] = counts.get(n.tuning_profile_id, 0) + 1
    return [_profile_out(p, did, active_pid, counts.get(p.id, 0))
            for p in state.store.list_profiles()]


@router.get("/profiles", response_model=list[ProfileOut])
def list_profiles(request: Request, _: None = Depends(require_auth)) -> list[ProfileOut]:
    return _profiles_out(get_state(request))


@router.get("/profiles/presets", response_model=list[ProfilePresetInfo])
def profile_presets(request: Request, _: None = Depends(require_auth)) -> list[ProfilePresetInfo]:
    return [ProfilePresetInfo(name=k, title=v["title"], fields=v["fields"])
            for k, v in PROFILE_PRESETS.items()]


@router.post("/profiles/validate", response_model=ProfileValidateOut)
def validate_profile_ep(body: ProfileIn, request: Request,
                        _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> ProfileValidateOut:
    """TN7: structural check, plus an xray -test of a node using this profile when one exists."""
    state = get_state(request)
    prof = TuningProfile(id=None, **body.model_dump())
    ok, err = validate_profile(prof)
    if not ok:
        return ProfileValidateOut(ok=False, error=err)
    nodes = state.store.list_nodes()
    if nodes:
        aid = state.store.get_setting("active_node_id")
        node = state.store.get_node(int(aid)) if aid else nodes[0]
        if node is not None:
            cfg = build_config(node, state.settings, profile=prof, tunneled_fetch=True)
            ok2, out = validate_config(cfg, state.xray_bin or state.settings.xray_bin)
            if not ok2:
                return ProfileValidateOut(ok=False, error=out)
    return ProfileValidateOut(ok=True)


@router.post("/profiles", response_model=ProfileOut)
def add_profile(body: ProfileIn, request: Request,
                _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> ProfileOut:
    state = get_state(request)
    prof = TuningProfile(id=None, **body.model_dump())
    ok, err = validate_profile(prof)
    if not ok:
        raise HTTPException(status_code=422, detail=err)
    pid = state.store.add_profile(prof)
    d = state.store.get_default_profile()
    return _profile_out(state.store.get_profile(pid), d.id if d else None,
                        _active_resolved_pid(state.store))


@router.put("/profiles/default", response_model=ProfileOut)
def set_default_profile(body: DefaultProfileIn, request: Request,
                        _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> ProfileOut:
    state = get_state(request)
    p = state.store.get_profile(body.id)
    if p is None:
        raise HTTPException(status_code=404, detail="profile not found")
    with apply_lock, state.store.transaction():
        before = _active_resolved_pid(state.store)
        state.store.set_default_profile(body.id)
        if _active_resolved_pid(state.store) != before:   # active node inherits default → re-apply
            _reapply_or_502(state)
    return _profile_out(p, body.id, _active_resolved_pid(state.store))


@router.patch("/profiles/{profile_id}", response_model=ProfileOut)
def update_profile(profile_id: int, body: ProfileUpdate, request: Request,
                   _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> ProfileOut:
    state = get_state(request)
    p = state.store.get_profile(profile_id)
    if p is None:
        raise HTTPException(status_code=404, detail="profile not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(p, k, v)
    ok, err = validate_profile(p)
    if not ok:
        raise HTTPException(status_code=422, detail=err)
    with apply_lock, state.store.transaction():
        state.store.update_profile(p)
        if _active_resolved_pid(state.store) == profile_id:  # edited live profile → re-apply
            _reapply_or_502(state)
    d = state.store.get_default_profile()
    return _profile_out(state.store.get_profile(profile_id), d.id if d else None,
                        _active_resolved_pid(state.store))


@router.post("/profiles/{profile_id}/apply-active")
def apply_profile_active(profile_id: int, request: Request,
                         _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    """TN6: assign this profile to the currently-active node and re-apply now (the 'panic' path)."""
    state = get_state(request)
    if state.store.get_profile(profile_id) is None:
        raise HTTPException(status_code=404, detail="profile not found")
    aid = state.store.get_setting("active_node_id")
    node = state.store.get_node(int(aid)) if aid else None
    if node is None:
        raise HTTPException(status_code=409, detail="no active node")
    with apply_lock, state.store.transaction():
        node.tuning_profile_id = profile_id
        state.store.update_node(node)
        _reapply_or_502(state)
    return {"ok": True, "node_id": node.id}


@router.delete("/profiles/{profile_id}")
def delete_profile(profile_id: int, request: Request,
                   _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    state = get_state(request)
    d = state.store.get_default_profile()
    if d is not None and d.id == profile_id:
        # the global default must always exist (nodes inherit it); reassign default first
        raise HTTPException(status_code=409, detail="cannot delete the default profile")
    with apply_lock, state.store.transaction():
        was_live = _active_resolved_pid(state.store) == profile_id   # active node uses it?
        state.store.delete_profile(profile_id)                       # referencing nodes → default
        if was_live:
            _reapply_or_502(state)
    return {"ok": True}


# --- routing ---
@router.get("/routing", response_model=RoutingOut)
def get_routing(request: Request, _: None = Depends(require_auth)) -> RoutingOut:
    return _routing_out(get_state(request))


def _clean_rules(rules) -> list[RoutingRule]:
    """Drop empty-value rules and dedup (type, value, action); re-position from 0."""
    seen, clean = set(), []
    for r in rules:
        v = (r.value or "").strip()
        if not v:
            continue
        key = (r.type, v, r.action)
        if key in seen:
            continue
        seen.add(key)
        clean.append(RoutingRule(id=None, position=len(clean), type=r.type, value=r.value,
                                 action=r.action, enabled=r.enabled, label=r.label))
    return clean


@router.put("/routing", response_model=RoutingOut)
def put_routing(body: RoutingIn, request: Request,
                _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> RoutingOut:
    state = get_state(request)
    rules = _clean_rules(body.rules)
    ok, err = validate_routing(rules, body.default_action)   # RC2: clear per-rule error, not raw xray
    if not ok:
        raise HTTPException(status_code=422, detail=err)
    with apply_lock, state.store.transaction():
        state.store.replace_routing(rules)
        state.store.set_setting("routing_default_action", body.default_action)
        state.store.set_setting("routing_domain_strategy", body.domain_strategy)
        # DB intent and live config are one logical transaction. apply_node restores last-good
        # runtime on error; escaping this context rolls the candidate rows back as well.
        res = reapply_active_node(state)
        if res is not None and not res.ok:
            raise HTTPException(status_code=502, detail=res.error)
    return _routing_out(state)


@router.get("/routing/presets", response_model=list[PresetInfo])
def routing_presets(request: Request, _: None = Depends(require_auth)) -> list[PresetInfo]:
    return [PresetInfo(name=k, title=v["title"]) for k, v in PRESETS.items()]


@router.post("/routing/validate", response_model=RoutingValidateOut)
def routing_validate(body: RoutingIn, request: Request,
                     _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> RoutingValidateOut:
    """RN1: dry-run — structural check, plus an `xray -test` of the full config when a node is
    active. Never persists or applies."""
    state = get_state(request)
    rules = _clean_rules(body.rules)
    ok, err = validate_routing(rules, body.default_action)
    if not ok:
        return RoutingValidateOut(ok=False, error=err)
    aid = state.store.get_setting("active_node_id")
    if aid:
        node = state.store.get_node(int(aid))
        if node is not None:
            cfg = build_config(node, state.settings, routing=(rules, body.default_action),
                               domain_strategy=body.domain_strategy)
            ok2, out = validate_config(cfg, state.xray_bin or state.settings.xray_bin)
            if not ok2:
                return RoutingValidateOut(ok=False, error=out)
    return RoutingValidateOut(ok=True)


@router.post("/routing/preset/{name}", response_model=RoutingOut)
def routing_preset(name: str, request: Request,
                   _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> RoutingOut:
    """RC1/RN2: stage a preset — return the current rules merged with the preset's new ones,
    WITHOUT persisting or applying. The user reviews and Saves to commit."""
    state = get_state(request)
    preset = preset_rules(name)
    if preset is None:
        raise HTTPException(status_code=404, detail=f"unknown preset {name!r}")
    existing = state.store.get_routing()
    have = {(r.type, r.value, r.action) for r in existing}
    merged = list(existing) + [r for r in preset if (r.type, r.value, r.action) not in have]
    for i, r in enumerate(merged):
        r.position = i
    return _routing_out(state, merged)


# --- node health (per-node snapshot; distinct from the open /api/health liveness) ---
@router.get("/node-health", response_model=list[NodeHealthOut])
def node_health(request: Request, _: None = Depends(require_auth)) -> list[NodeHealthOut]:
    state = get_state(request)
    return [_health_out(h) for h in state.store.list_health()]


def _scoped_nodes(store, scope: str | None) -> list:
    """NR2: resolve a probe scope — None/'all' = every node, 'servers' = manual nodes,
    or a subscription id string = that sub's nodes."""
    nodes = store.list_nodes()
    if not scope or scope == "all":
        return nodes
    if scope == "servers":
        return [n for n in nodes if n.subscription_id is None]
    try:
        sid = int(scope)
    except ValueError:
        raise HTTPException(status_code=422, detail="scope must be all, servers, or a subscription id")
    if store.get_subscription(sid) is None:
        raise HTTPException(status_code=422, detail="subscription scope not found")
    return [n for n in nodes if n.subscription_id == sid]


def _probe_sweep(store, nodes, probe_one, assign, record_http=False) -> list[NodeHealthOut]:
    """Probe the given nodes concurrently, persist (preserving the fields the other sweep
    owns), and return the full updated health list."""
    ts = datetime.now(timezone.utc).isoformat()
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(nodes)))) as ex:
        results = list(ex.map(lambda n: (n.id, probe_one(n)), nodes))
    for nid, (ok, ms) in results:
        h = store.get_health(nid) or NodeHealth(node_id=nid)
        assign(h, ok, ms)
        h.checked_at = ts
        store.upsert_health(h)
        if record_http and ok and ms is not None:
            store.record_latency(nid, ms)   # NN4: HTTPS latency trend
    return [_health_out(h) for h in store.list_health()]


@router.post("/probe/tcp", response_model=list[NodeHealthOut])
def probe_tcp(request: Request, scope: str | None = None,
              _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> list[NodeHealthOut]:
    """TCP-ping nodes (reachability + latency) on demand → updates the TCP column. `scope`
    limits the sweep to a subscription / manual group (default all)."""
    store = get_state(request).store
    def assign(h: NodeHealth, ok, ms): h.last_tcp_ok, h.last_tcp_ms = ok, ms
    return _probe_sweep(store, _scoped_nodes(store, scope),
                        lambda n: probe.tcp_ping(n.address, n.port, timeout=2.0,
                                                 allow_private=probe.operator_added(n)), assign)


@router.post("/probe/http", response_model=list[NodeHealthOut])
def probe_http(request: Request, scope: str | None = None,
               _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> list[NodeHealthOut]:
    """HTTPS-handshake each node directly (not through the tunnel) → HTTP column. `scope`
    limits the sweep to a subscription / manual group (default all)."""
    store = get_state(request).store
    def assign(h: NodeHealth, ok, ms): h.last_http_ok, h.last_http_ms = ok, ms
    return _probe_sweep(store, _scoped_nodes(store, scope),
                        lambda n: probe.http_ping(n.address, n.port, n.sni, timeout=3.0,
                                                  allow_private=probe.operator_added(n)),
                        assign, record_http=True)


@router.post("/nodes/{node_id}/probe", response_model=NodeHealthOut)
def probe_node(node_id: int, request: Request,
               _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> NodeHealthOut:
    """Per-node 'T': TCP + direct HTTPS + a real request *through* this node — and persist them.

    The real request ALWAYS goes through a throwaway xray, even for the active node: reusing the
    live tunnel's proxy here degrades the user's active connection for the duration of the probe
    (reverts the earlier NR3 reuse-live-proxy shortcut)."""
    state = get_state(request)
    store = state.store
    node = store.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="node not found")
    probe_url = store.get_setting("health_probe_url") or SETTINGS_DEFAULTS["health_probe_url"]
    # v6 egress: only when the IPv6 tunnel is on (a v6-only echo → the node's v6 egress, or None)
    url6 = (store.get_setting("health_probe_url6") or SETTINGS_DEFAULTS["health_probe_url6"]
            if (store.get_setting("ipv6_enabled") or "0") == "1" else None)
    # All three probes get the node's provenance, like the background sweep: this button is how
    # an operator checks their own LAN node, and a strict probe here would both refuse it and
    # overwrite the sweep's honest result with a "dead" one. A feed node stays strict.
    allow_private = probe.operator_added(node)
    real_ok, real_ms, egress, egress6 = probe.real_through_node(
        node, state.xray_bin, probe_url, probe_url6=url6, allow_private=allow_private)
    h = store.get_health(node_id) or NodeHealth(node_id=node_id)
    h.last_tcp_ok, h.last_tcp_ms = probe.tcp_ping(node.address, node.port, timeout=3.0,
                                                  allow_private=allow_private)
    h.last_http_ok, h.last_http_ms = probe.http_ping(node.address, node.port, node.sni,
                                                     timeout=4.0, allow_private=allow_private)
    h.last_real_ok, h.last_real_ms, h.egress_ip, h.egress_ip6 = real_ok, real_ms, egress, egress6
    h.checked_at = datetime.now(timezone.utc).isoformat()
    store.upsert_health(h)
    if h.last_http_ok and h.last_http_ms is not None:
        store.record_latency(node_id, h.last_http_ms)   # NN4
    return _health_out(store.get_health(node_id))


_LOG_SOURCES = {"xray-error": "xray_error_log", "xray-access": "xray_access_log", "app": "app_log"}


# --- logs (read-only tail) ---
@router.get("/logs")
def get_logs(request: Request, source: str = "xray-error", lines: int = 200,
             _: None = Depends(require_auth)) -> dict:
    attr = _LOG_SOURCES.get(source)
    if attr is None:
        raise HTTPException(status_code=400, detail="unknown log source")
    path = getattr(get_state(request).settings, attr)
    return {"source": source, "lines": logs_mod.tail(path, max(1, min(lines, 1000)))}


# --- backup / restore ---
@router.get("/backup")
def get_backup(request: Request, _: None = Depends(require_auth)) -> dict:
    doc = backup_mod.export_state(get_state(request).store)
    # Hand back nothing the panel's own restore would refuse. A downloaded file that turns out to
    # be unrestorable is discovered during a recovery, when it is the only copy left; a 500 here
    # is discovered now, while the state it describes is still live and fixable in the UI.
    try:
        backup_mod.validate_document(doc)
    except ValueError as exc:
        raise HTTPException(status_code=500,
                            detail=f"stored state is not restorable: {exc}") from exc
    return doc


@router.post("/restore")
def post_restore(body: dict, request: Request,
                 _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    # minimal structural contract before a full-state overwrite: a real backup is a dict carrying
    # a schema_version marker. Rejects a mis-picked settings-export / truncated file up front.
    if not isinstance(body, dict) or "schema_version" not in body:
        raise HTTPException(status_code=400, detail="not a valid backup file")
    try:
        result = restore_backup(get_state(request), body)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid backup: {exc}")
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    # The snapshot of what this restore replaced — name it, or the operator has no way to know
    # an undo exists.
    return {"ok": True, "restored": result.summary, "runtime": "disconnected",
            "pre_restore_snapshot": result.snapshot}


# --- subscriptions ---
@router.get("/subs", response_model=list[SubscriptionOut])
def list_subs(request: Request, _: None = Depends(require_auth)) -> list[SubscriptionOut]:
    state = get_state(request)
    counts = state.store.node_counts_by_sub()   # one query, not one-per-sub (OB4)
    return [_sub_out(state, s, counts.get(s.id, 0)) for s in state.store.list_subscriptions()]


@router.post("/subs", response_model=SubscriptionOut)
def add_sub(body: SubscriptionIn, request: Request,
            _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> SubscriptionOut:
    state = get_state(request)
    _check_sub_url(body.url)
    _check_profile_id(state.store, body.default_profile_id)
    injection = body.injection if body.injection is not None else default_injection()
    sid = state.store.add_subscription(Subscription(
        id=None, name=body.name, url=body.url, injection=injection,
        interval_sec=_clamp_interval(body.interval_sec),
        enabled=body.enabled, default_profile_id=body.default_profile_id))
    return _sub_out(state, state.store.get_subscription(sid))


@router.patch("/subs/{sub_id}", response_model=SubscriptionOut)
def update_sub(sub_id: int, body: SubscriptionPatch, request: Request,
               _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> SubscriptionOut:
    state = get_state(request)
    sub = state.store.get_subscription(sub_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="subscription not found")
    # exclude_unset so an explicit `default_profile_id: null` clears the inherited profile
    # rather than being dropped (mirrors update_node).
    patch = body.model_dump(exclude_unset=True)
    if patch.get("url") is not None and patch["url"] != sub.url:
        _check_sub_url(patch["url"])
    if "default_profile_id" in patch:
        _check_profile_id(state.store, patch["default_profile_id"])
    for k, v in patch.items():
        setattr(sub, k, v)
    sub.interval_sec = _clamp_interval(sub.interval_sec)
    state.store.update_subscription(sub)
    return _sub_out(state, state.store.get_subscription(sub_id))


@router.delete("/subs/{sub_id}")
def delete_sub(sub_id: int, request: Request,
               _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    state = get_state(request)
    # 404 on a missing id, like delete_node / delete_token / delete_rw_client. Reporting success
    # for an id that was never there hides a stale UI or a wrong id from whoever called.
    if state.store.get_subscription(sub_id) is None:
        raise HTTPException(status_code=404, detail="subscription not found")
    state.store.delete_subscription(sub_id)
    return {"ok": True}


@router.post("/subs/{sub_id}/refresh")
def refresh_sub(sub_id: int, request: Request,
                _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    state = get_state(request)
    sub = state.store.get_subscription(sub_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="subscription not found")
    return service.refresh(state, sub)


@router.post("/subs/preview", response_model=PreviewOut)
def preview_sub(body: PreviewIn, request: Request,
                _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> PreviewOut:
    state = get_state(request)
    injection = body.injection if body.injection is not None else default_injection()
    tokens = host_tokens(service.machine_id(), app_secret=state.settings.session_secret,
                         subscription_id=f"preview:{body.url}")
    req = build_request(body.url, injection, tokens)
    return PreviewOut(method=req.method, url=req.url, headers=req.headers, query=req.query)


@router.post("/subs/preview-nodes", response_model=PreviewNodesOut)
def preview_sub_nodes(body: PreviewIn, request: Request,
                      _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> PreviewNodesOut:
    """N1: dry-run — fetch + parse without persisting, so a bad URL/token/format is caught
    before adding/saving. Uses the tunnel when one is up, like a real refresh."""
    state = get_state(request)
    injection = body.injection if body.injection is not None else default_injection()
    tokens = host_tokens(service.machine_id(), app_secret=state.settings.session_secret,
                         subscription_id=f"preview:{body.url}")
    try:
        text, _path, _headers = fetch(body.url, injection, tokens, proxy=service.tunnel_proxy(state))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"fetch failed: {exc}")
    try:
        fmt = detect(text)
        nodes = parse_subscription(text, limit=service.MAX_NODES + 1)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"parse failed: {exc}")
    return PreviewNodesOut(
        format=fmt, count=min(len(nodes), service.MAX_NODES),
        returned_count=min(len(nodes), service.MAX_NODES, 200),
        truncated=min(len(nodes), service.MAX_NODES) > 200,
        nodes=[PreviewNodeOut(name=n.name, address=n.address, port=n.port,
                              transport=n.transport, network=n.network, security=n.security)
               for n in nodes[:200]])


@router.post("/subs/refresh-all", response_model=RefreshAllOut)
def refresh_all_subs(request: Request,
                     _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> RefreshAllOut:
    """N3: refresh every enabled subscription now. Disabled subs are skipped."""
    state = get_state(request)
    results = []
    for sub in state.store.list_subscriptions():
        if sub.enabled:
            result = service.refresh(state, sub)
            results.append({"id": sub.id, "name": sub.name, "ok": bool(result.get("ok")),
                            "status": result.get("status"), "error": result.get("error")})
    succeeded = sum(result["ok"] for result in results)
    return {"attempted": len(results), "succeeded": succeeded,
            "failed": len(results) - succeeded, "results": results}


@router.post("/nodes/reorder")
def reorder_nodes(body: ReorderIn, request: Request,
                  _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    """N8: persist a new order (position = list index) for the given node ids."""
    get_state(request).store.reorder_nodes(body.ids)
    return {"ok": True}


@router.post("/nodes/detach")
def detach_nodes(body: DetachIn, request: Request,
                 _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    """NN3: detach the given nodes from their subscription (→ manual Servers)."""
    get_state(request).store.detach_nodes(body.ids)
    return {"ok": True}


@router.post("/nodes/validate", response_model=NodeValidateOut)
def validate_node(body: NodeValidateIn, request: Request,
                  _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> NodeValidateOut:
    """NN10: pre-flight — build this node's config and run `xray -test` without connecting,
    so a bad reality/xhttp/tls combo is caught before Connect."""
    state = get_state(request)
    node = Node(id=None, name=body.name, address=body.address, port=body.port,
                uuid=body.uuid, transport=body.transport, security=body.security,
                sni=body.sni, public_key=body.public_key, short_id=body.short_id,
                fingerprint=body.fingerprint, path=body.path, host=body.host,
                mode=body.mode, alpn=body.alpn, tuning_profile_id=body.tuning_profile_id)
    _check_profile_id(state.store, body.tuning_profile_id)
    cfg = build_node_config(node, state.settings, state.store)
    ok, out = validate_config(cfg, state.xray_bin or state.settings.xray_bin)
    return NodeValidateOut(ok=ok, error="" if ok else out)


@router.post("/nodes/import", response_model=ImportNodesOut)
def import_nodes(body: ImportNodesIn, request: Request,
                 _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> ImportNodesOut:
    """N4: parse pasted subscription text (base64 / clash / json) and add the nodes as manual
    servers (subscription_id NULL), skipping ones already present by identity."""
    state = get_state(request)
    try:
        fmt = detect(body.text)
        parsed = parse_subscription(body.text, limit=service.MAX_NODES + 1)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"parse failed: {exc}")
    added = 0
    # One transaction for the whole batch: these inserts used to autocommit one at a time, so a
    # failure partway through left the earlier nodes behind and still answered 500 — an import the
    # operator was told had failed had in fact half-happened. All or nothing now.
    #
    # And clamp every parsed node, the same way a subscription refresh does (subs/reconcile) —
    # this is the other door untrusted feed strings come through, and unbounded ones bloat the DB
    # and every config render from here on.
    try:
        with state.store.transaction():
            for p in parsed[:service.MAX_NODES]:
                clamp_node_fields(p)
                if state.store.get_node_by_identity(
                    None, p.address, p.port, p.uuid, p.path, p.sni, p.short_id) is not None:
                    continue
                p.id = None
                p.subscription_id = None
                p.stale = False
                state.store.add_node(p)
                added += 1
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail=f"import rejected, nothing was added: {exc}") from exc
    return ImportNodesOut(added=added, total=len(parsed), format=fmt)


@router.post("/connect-best")
def connect_best(body: ConnectBestIn, request: Request,
                 _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    """N9: connect to the healthiest non-stale node in a scope (a subscription, or manual
    when subscription_id is null), using the latest probe data."""
    state = get_state(request)
    node = _pick_best_node(state.store, body.subscription_id)
    if node is None:
        raise HTTPException(status_code=404, detail="no connectable node in this group")
    res = apply_node(node, state.settings, state.supervisor, state.net,
                     store=state.store, xray_bin=state.xray_bin)
    if not res.ok:
        raise HTTPException(status_code=502, detail=res.error)
    return {"ok": True, "node_id": node.id}


# --- settings (global toggles) ---
@router.get("/settings", response_model=SettingsOut)
def get_settings(request: Request, _: None = Depends(require_auth)) -> SettingsOut:
    return _settings_out(get_state(request))


# Settings that are baked into the xray config → changing them needs a live re-apply.
_SETTINGS_CONFIG_KEYS = {"tunneled_fetch", "dns_intercept", "stats_enabled", "stats_api_port"}
# Settings the Settings screen owns (reset target — excludes routing-owned keys).
_SETTINGS_RESET_KEYS = ("tunneled_fetch", "subs_auto_switch", "dns_intercept", "health_enabled",
                        "health_sweep_enabled", "health_interval", "health_active_interval",
                        "health_hysteresis", "health_probe_url", "failover_enabled",
                        "failover_cooldown", "stats_enabled", "stats_api_port",
                        "traffic_sample_ms", "session_timeout_min", "auto_backup_enabled")


def _validate_settings(state, data: dict) -> None:
    """SC2: reject out-of-range values that would break the runtime (busy loops, bad ports).

    The bounds live in `config.SETTINGS_INT_BOUNDS` / `SETTINGS_CHOICES` and are shared with the
    backup validator, because the same keys are equally writable by a restored document — which
    used to skip these checks entirely.
    """
    try:
        validate_setting_values(
            data, reserved_ports=(state.settings.tproxy_port, state.settings.local_proxy_port))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/settings", response_model=SettingsOut)
def put_settings(body: SettingsIn, request: Request,
                 _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> SettingsOut:
    state = get_state(request)
    data = body.model_dump(exclude_none=True)
    _validate_settings(state, data)
    with apply_lock, state.store.transaction():
        for k, v in data.items():
            state.store.set_setting(k, ("1" if v else "0") if isinstance(v, bool) else str(v))
        if _SETTINGS_CONFIG_KEYS & data.keys():   # config-affecting toggle → re-apply live
            _reapply_or_502(state)
    if "stats_api_port" in data and state.stats_client is not None:
        state.stats_client.reconfigure(f"127.0.0.1:{data['stats_api_port']}")
    return _settings_out(state)


@router.post("/settings/reset", response_model=SettingsOut)
def reset_settings(request: Request,
                   _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> SettingsOut:
    """SN1: restore the Settings-screen knobs to their defaults (routing-owned keys untouched)."""
    state = get_state(request)
    with apply_lock, state.store.transaction():
        for k in _SETTINGS_RESET_KEYS:
            state.store.set_setting(k, SETTINGS_DEFAULTS[k])
        _reapply_or_502(state)
    if state.stats_client is not None:
        state.stats_client.reconfigure(
            f"127.0.0.1:{int(SETTINGS_DEFAULTS['stats_api_port'])}")
    return _settings_out(state)


@router.get("/diagnostics", response_model=DiagnosticsOut)
def diagnostics(request: Request, _: None = Depends(require_auth)) -> DiagnosticsOut:
    """SN8: at-a-glance support info — app/xray version, uptime, DB size, disk."""
    state = get_state(request)
    from pi_gw_panel import __version__
    try:
        out = subprocess.run([state.xray_bin or state.settings.xray_bin, "-version"],
                             capture_output=True, text=True, timeout=5)
        text = (out.stdout or out.stderr).strip()
        xray_v = text.splitlines()[0] if text else "unknown"
    except Exception:
        xray_v = "unavailable"
    db = state.settings.db_path
    db_bytes = os.path.getsize(db) if os.path.exists(db) else 0
    du = shutil.disk_usage(state.settings.data_dir)
    stats_status = state.stats_client.status() if state.stats_client is not None else {}
    return DiagnosticsOut(app_version=__version__, xray_version=xray_v,
                          uptime_sec=int(time.time() - _START_TIME), db_path=db, db_bytes=db_bytes,
                          disk_free_bytes=du.free, disk_total_bytes=du.total,
                          stats_last_ok_at=stats_status.get("last_ok_at"),
                          stats_error=stats_status.get("last_error", ""),
                          stats_fail_count=stats_status.get("fail_count", 0))


# --- network (editable Pi net config + kill-switch + live status + router guidance) ---
@router.get("/network", response_model=NetworkOut)
def get_network(request: Request, _: None = Depends(require_auth)) -> NetworkOut:
    return _network_out(get_state(request))


@router.put("/network", response_model=NetworkOut)
def put_network(body: NetworkIn, request: Request,
                _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> NetworkOut:
    state = get_state(request)
    data = body.model_dump(exclude_none=True)
    # These values are interpolated verbatim into the nft ruleset and dnsmasq.conf; a newline/quote
    # would inject arbitrary nft rules or dnsmasq directives (DNS-leak `server=`, kill-switch removal),
    # and a merely malformed value silently breaks the live segment. Validate strictly at the boundary
    # — including the cross-field collisions that would move the segment onto the management leg,
    # which are refused here, outside the lock and before a single value is written.
    _validate_net_fields(data, state)
    from pi_gw_panel.net_control import provision
    ipv6_changed = False
    running_active = False
    with apply_lock:
        # Committed OUTSIDE the transaction on purpose: a record that rolls back with the
        # transaction it is meant to clean up after would never be readable when it is needed.
        candidate = provision.provision_candidate(state, data)
        provision.record_provision_candidate(state.store, candidate)
        installed: dict = {}
        try:
            with state.store.transaction():
                for k in _NET_EDITABLE:
                    if k in data:
                        state.store.set_setting(k, data[k])
                if "kill_switch_enabled" in data:
                    was = (state.store.get_setting("kill_switch_enabled") or "1") == "1"
                    now_on = bool(data["kill_switch_enabled"])
                    state.store.set_setting("kill_switch_enabled", "1" if now_on else "0")
                    if now_on != was:
                        conn_events.record(
                            state.store, "kill-switch", "enabled" if now_on else "disabled")
                if "lan_access_enabled" in data:
                    was_lan = (state.store.get_setting("lan_access_enabled")
                               or ("1" if state.settings.lan_access else "0")) == "1"
                    lan_on = bool(data["lan_access_enabled"])
                    state.store.set_setting("lan_access_enabled", "1" if lan_on else "0")
                    if lan_on != was_lan:
                        conn_events.record(
                            state.store, "lan-access", "enabled" if lan_on else "disabled")
                if "ipv6_enabled" in data:
                    was6 = (state.store.get_setting("ipv6_enabled") or "0") == "1"
                    on6 = bool(data["ipv6_enabled"])
                    state.store.set_setting("ipv6_enabled", "1" if on6 else "0")
                    if on6 != was6:
                        ipv6_changed = True
                        conn_events.record(
                            state.store, "ipv6", "enabled" if on6 else "disabled")

                provision_result = provision.host_provision(state)
                # Read back before anything can roll it away: this is exactly what the pass put
                # on the host, including the v6 prefix only it could resolve.
                installed = provision.managed_host_state(state.store)
                if getattr(provision_result, "ok", True) is False:
                    raise RuntimeError(
                        f"host provisioning failed: {provision_result.error or 'unknown error'}")
                # Toggling IPv6 changes Xray itself; other fields only change host rules.
                running_active = bool(
                    state.supervisor.status().get("running")
                    and state.store.get_setting("active_node_id"))
                if ipv6_changed and running_active:
                    _reapply_or_502(state)
                else:
                    net_result = sync_net(state)
                    if not net_result.ok:
                        raise RuntimeError(net_result.error or "network apply failed")
        except Exception as exc:
            # The DB transaction has rolled back here. Reconcile the host from that previous
            # source of truth before reporting the candidate failure.
            recovery: list[str] = []
            try:
                restored_host = provision.host_provision(state)
                if getattr(restored_host, "ok", True) is False:
                    recovery.append(restored_host.error or "host provisioning restore failed")
            except Exception as restore_exc:
                recovery.append(f"host provisioning restore raised: {restore_exc}")
            # The restore reconciles the OLD interface only, so whatever the candidate pass put
            # somewhere else has to be named and removed explicitly.
            undone = provision.undo_provision_candidate(state, candidate, installed)
            recovery.extend(undone.actions)
            # Cleared only once nothing is left outstanding: a leftover this pass could not prove
            # gone stays recorded so the next boot retries it (`resume_pending_provision_undo`).
            if candidate and not undone.unresolved:
                provision.clear_provision_candidate(state.store)
            try:
                if ipv6_changed and running_active:
                    restored_runtime = reapply_active_node(state)
                    if restored_runtime is not None and not restored_runtime.ok:
                        recovery.append(restored_runtime.error)
                else:
                    restored_net = sync_net(state)
                    if not restored_net.ok:
                        recovery.append(restored_net.error or "network restore failed")
                        # A failed restore is not a state, it is an unknown: `sync_net` may have
                        # got part-way through installing the tproxy ruleset, so nothing here is
                        # proven fail-closed. This site used to only APPEND the error — the one
                        # recovery path that reported its failure with no guard attempt at all —
                        # leaving client→WAN in whatever shape the failed apply left it.
                        #
                        # So: assert the guard. If even that cannot be installed, do not also
                        # leave the tunnel up on rules that were never proven down — take xray
                        # down and say so. The order is the opposite of the rollback's on purpose:
                        # there xray is running a config the store no longer describes, while here
                        # the tunnel is still the legitimate one and only an unguardable network
                        # justifies ending it.
                        guard = stop_net(state.settings, state.net, state.store)
                        if not guard.ok:
                            recovery.append(f"fail-closed recovery failed: "
                                            f"{guard.error or 'network guard failed'}")
                            _, stop_detail = _bounded_stop(state.supervisor)
                            if stop_detail:
                                recovery.append(stop_detail)
            except Exception as restore_exc:
                recovery.append(f"runtime restore raised: {restore_exc}")
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            if recovery:
                detail = f"{detail}; recovery: {'; '.join(recovery)}"
            raise HTTPException(status_code=502, detail=detail) from exc
        if candidate:                       # committed, so nothing is left pointing at an undo
            provision.clear_provision_candidate(state.store)
    return _network_out(state)


# --- api tokens (programmatic REST access: read / read-write) ---
@router.get("/tokens", response_model=list[TokenOut])
def list_tokens(request: Request, _: None = Depends(require_auth)) -> list[TokenOut]:
    return [TokenOut(**t) for t in get_state(request).store.list_tokens()]


@router.post("/tokens", response_model=TokenCreatedOut, status_code=201)
def create_token(body: TokenCreateIn, request: Request,
                 _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> TokenCreatedOut:
    full, token_hash, prefix = tokens.generate()
    if body.expires_at is not None and body.expires_at <= int(time.time()):
        raise HTTPException(status_code=422, detail="expires_at must be in the future")
    row = get_state(request).store.create_token(
        body.name, body.scope, token_hash, prefix, expires_at=body.expires_at)
    return TokenCreatedOut(**row, token=full)


@router.delete("/tokens/{token_id}", status_code=204)
def delete_token(token_id: int, request: Request,
                 _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> None:
    if not get_state(request).store.delete_token(token_id):
        raise HTTPException(status_code=404, detail="token not found")


# --- audit log (N2: who changed what, when) ---
@router.get("/audit", response_model=list[AuditEntryOut])
def audit_log(request: Request, limit: int = 100,
              _: None = Depends(require_auth)) -> list[AuditEntryOut]:
    """Newest-first log of successful mutations (recorded by the audit middleware)."""
    return [AuditEntryOut(**e) for e in get_state(request).store.list_audit(limit)]


# --- road-warrior inbound (reach the gateway + its LAN from outside) ---
def _rw_default_nets(state) -> list[str]:
    """The subnets a remote client should route into the tunnel, DERIVED from the live net
    plan rather than hardcoded — a hardcoded 192.168.1.0/24 would start lying the moment the
    addressing changes. Same helper the nft renderer uses."""
    plan = NetPlan.from_store(state.store, state.settings)
    nets, seen = [], set()
    for ip in (plan.mgmt_ip, plan.segment_ip):
        net = net24(ip) if ip else ""
        if net and net not in seen:
            seen.add(net)
            nets.append(net)
    return nets


def _rw_nets(state) -> list[str]:
    override = rw_mod.parse_nets(state.store.get_setting("rw_routed_nets") or "")
    return override or _rw_default_nets(state)


def _rw_credentials(store) -> dict:
    """Everything that decides WHO the running inbound accepts and WHERE it accepts them.

    Snapshotted before and after a save so the narrowing test below reads the values that were
    actually stored, instead of a hand-kept list of "fields that count" which drifts the moment
    a new one is added.
    """
    def get(key: str) -> str:
        value = store.get_setting(key)
        return rw_mod.DEFAULTS.get(key, "") if value is None else value

    def port(raw: str) -> str:
        try:
            return str(rw_mod.validate_port(raw))
        except ValueError:                      # malformed stored value: compare it verbatim
            return raw.strip()

    return {
        "enabled": get("rw_enabled") == "1",
        "port": port(get("rw_port")),
        # Lowercased on purpose: Reality matches short ids by BYTES and SNI case-insensitively,
        # so `AB12` → `ab12` is the same credential, not a rotation.
        "short_ids": {s.lower() for s in rw_mod.parse_csv(get("rw_short_ids"))},
        "server_names": {n.lower() for n in rw_mod.parse_csv(get("rw_server_names"))},
        "private_key": (store.get_setting("rw_private_key") or "").strip(),
    }


def _rw_narrows(before: dict, after: dict) -> bool:
    """Whether a save TAKES AWAY access the live inbound was granting.

    Every credential counts, not just the private key. Rotating or dropping a short id, dropping
    a server name, and moving the port each cut off a client that can connect right now — and
    rotating a short id is the most natural thing an operator reaches for after losing a device.
    Misclassifying one of those as a widening change is not cosmetic: it takes the "stored, and
    picked up on the next connect" path, which with no active node leaves the running inbound
    accepting the very credential the operator just revoked.

    Adding a short id or a server name grants and never revokes, so a superset is not a
    narrowing. `before` with no key granted nothing at all — resolve() emits no inbound without
    one — so setting the first key is a grant, not a rotation.
    """
    if not before["enabled"] or not before["private_key"]:
        return False
    return (not after["enabled"]
            or after["private_key"] != before["private_key"]
            or after["port"] != before["port"]
            or not before["short_ids"] <= after["short_ids"]
            or not before["server_names"] <= after["server_names"])


def _rw_out(state, *, revocation: str = "") -> RwOut:
    store = state.store

    def get(key: str) -> str:
        value = store.get_setting(key)
        return rw_mod.DEFAULTS.get(key, "") if value is None else value

    # Read defensively: values are validated on write, so anything malformed here came from a
    # hand-edited DB or a foreign backup. Returning 500 would make the screen unreachable — the
    # one place the operator could fix it. Report the damage in-band instead.
    state_error = ""
    try:
        port = rw_mod.validate_port(get("rw_port"))
    except ValueError as exc:
        port, state_error = int(rw_mod.DEFAULTS["rw_port"]), str(exc)
    try:
        hosts = rw_mod.get_hosts(store)
    except ValueError as exc:
        hosts, state_error = {}, f"{state_error}; {exc}".lstrip("; ")
    return RwOut(
        enabled=get("rw_enabled") == "1",
        port=port,
        state_error=state_error,
        dest=get("rw_dest"),
        server_names=get("rw_server_names"),
        short_ids=get("rw_short_ids"),
        public_key=get("rw_public_key"),
        endpoint=get("rw_endpoint"),
        has_private_key=bool((get("rw_private_key") or "").strip()),
        hosts=hosts,
        routed_nets=_rw_nets(state),
        routed_nets_override=get("rw_routed_nets"),
        clients=[RwClientOut(**c) for c in rw_mod.get_clients(store)],
        live=_rw_serving(state),
        revocation=revocation,
    )


def _rw_apply(state) -> None:
    """Rebuild+apply so a change reaches the live inbound.

    With no active node there is nothing to build a config from, so the settings are stored and
    picked up on the next connect. The inbound itself does NOT need a reachable node to serve
    LAN access: `private → direct` is independent of the proxy outbound, and disconnect leaves
    xray running — which is exactly why the reported liveness is read back off the running
    config (see _rw_serving) instead of inferred from whether this rebuilt anything.

    Only correct for changes that GRANT or widen access. A revocation must go through
    _rw_revoke — "stored, picked up later" is a silent no-op when the point was to cut a lost
    device off right now.
    """
    res = reapply_active_node(state)
    if res is not None and not res.ok:
        raise HTTPException(status_code=502, detail=res.error)


def _rw_rebuild_node(state):
    """The node a revocation must rebuild the on-disk config from WITHOUT reconnecting.

    The active one when a node is selected — `/xray/stop` keeps the selection, so that is
    exactly what the next `/xray/start` rebuilds the file from, and writing it now only makes
    the file agree with itself sooner. Otherwise the node the last apply ran from:
    `disconnect` records it and leaves xray running on that very config, so it is what the
    live config already describes.

    Only ever used to WRITE a config; deciding whether the running process is told to pick
    that config up is a separate question with a separate answer (see _rw_revoke).
    """
    for key in ("active_node_id", "prev_active_node_id"):
        raw = state.store.get_setting(key)
        if not raw:
            continue
        try:
            node = state.store.get_node(int(raw))
        except (TypeError, ValueError):
            node = None
        if node is not None:
            return node
    return None


def _rw_config_shape(cfg) -> bool:
    """Whether `cfg` is shaped like an xray config we can reason about — an object whose
    `inbounds` is a list of objects. Anything else cannot be reconciled or compared, because we
    cannot tell what removing the remote-access inbound from it would leave behind."""
    inbounds = cfg.get("inbounds") if isinstance(cfg, dict) else None
    return isinstance(inbounds, list) and all(isinstance(i, dict) for i in inbounds)


def _rw_config_from_disk(state) -> dict | None:
    """The live config parsed, or None when it cannot be read as an xray config. The one reader
    behind both the sanitize and the excess check, so they cannot disagree about what the file
    says; they differ only in what they make of None, and deliberately (see each)."""
    try:
        with open(state.settings.config_path) as f:
            cfg = json.load(f)
    except Exception:
        return None
    return cfg if _rw_config_shape(cfg) else None


def _rw_sanitized_config(state, on_disk: dict | None) -> dict | None:
    """The config ON DISK with its remote-access inbound brought in line with what the store
    says AFTER the revocation — or None when the live file could not be read as an xray config.

    Takes the parsed live config rather than reading the file itself, because the caller has to
    compare what this produces against what it was produced FROM (see `_rw_write_config`), and two
    reads are two observations: the start guard runs this on the supervisor's own thread without
    `apply_lock`, so a file that changed between them would make the comparison meaningless. Copied
    before it is edited — `_rw_reconciled` works in place — so the caller's config stays the
    untouched `before` picture.

    REMOVING something from a config needs no node. Rebuilding from a node is a whole-config
    render and therefore needs one; requiring it made "the operator deleted the node after
    disconnecting" — the ordinary aftermath of losing a device — a state where the revocation
    could not clean the file at all, so the credential sat in it waiting for the next bare
    `/xray/start` to serve it again. Editing the file we already have has no such precondition.

    The credentials are exactly one object: `rw_inbound_block` is where every remote-access
    credential lives, so replacing it with what `resolve()` now returns removes the revoked
    client, the rotated short id, the moved port and the dropped server name alike — whatever the
    store no longer grants. `resolve()` returning None means there is nothing left to serve
    (feature off, key gone, no enabled clients) and the inbound is dropped outright.

    The inbound is not the only thing the feature owns, though, and reconciling it alone left
    three live fragments behind (see `_rw_reconcile_lan`): the `dns.hosts` mapping, the
    `direct-lan` outbound, and the exact-domain rule pointing at it. Two consequences, both
    real: a host removal shipped together with a credential rotation reported success while the
    old name→address mapping stayed live, and turning remote access OFF left the mapped names
    still routed out an untunnelled freedom outbound — for TPROXY clients too, since that rule
    is not scoped to `rw-in`. Same family as the `.com` suffix leak. So the reconcile covers
    every fragment the builder emits for this feature, and nothing else: still a surgical
    removal of access, not a re-render that would drag in unrelated store changes the operator
    never asked to apply.

    Malformed stored settings resolve to None rather than raising — the same "degrade to feature
    off" rule the apply path uses, which here is also the fail-safe direction.

    Returns a config to be written through ConfigManager (validated there); None when the live
    file is missing, unparseable, or not shaped like a config, since then we cannot know what
    removing the inbound would leave behind. `_rw_write_config` falls back to a node rebuild for
    exactly that case — a file we cannot read is the one thing a rebuild is still better at.
    """
    return None if on_disk is None else _rw_reconciled(state, copy.deepcopy(on_disk))


def _rw_reconciled(state, cfg: dict) -> dict:
    """`cfg`, edited in place, with every remote-access fragment brought in line with what the
    store grants now. Returns the same object.

    Split out of `_rw_sanitized_config` because the live file is not the only config that has to
    pass this test before it is served: `POST /rollback` promotes one that is by definition older
    than the store, and it has to be reconciled BEFORE it is installed rather than after — a
    credential-bearing file that is already live is not made harmless by the fact that we noticed.
    Same edit, same guarantee, applied to a config in hand instead of one on disk.

    Callers must have checked `_rw_config_shape` (both entry points do): the reconcile edits
    `inbounds` and cannot invent a shape it did not get.
    """
    try:
        rw = rw_mod.resolve(state.store)
    except ValueError:
        rw = None
    kept = [i for i in cfg["inbounds"] if i.get("tag") != RW_TAG]
    if rw is not None:
        kept.append(rw_inbound_block(rw))
    else:
        _rw_drop_routing_refs(cfg)
    cfg["inbounds"] = kept
    _rw_reconcile_lan(cfg, (rw or {}).get("hosts") or {}, state.settings)
    return cfg


def _rw_reconcile_lan(cfg, hosts: dict, settings) -> None:
    """Bring the LAN-by-name fragments in line with `hosts`, in place.

    `dns.hosts`, the `direct-lan` outbound and the one exact-domain rule naming it are emitted
    only by the remote-access feature and only together, so reconciling them is the same job as
    reconciling the inbound — and skipping it was not cosmetic. The rule is NOT scoped to
    `rw-in`, so it steers tproxy traffic as well: with remote access turned off, a stale
    `full:nas.example.com → direct-lan` kept sending that name out a plain freedom outbound for
    every LAN client, past the tunnel. And a mapping removed in the same save as a credential
    rotation stayed live while the response said `rebuilt`.

    Removal first and unconditionally, re-emission only when there is something to emit — so
    every exit from here either matches what `build_config` would produce for `hosts` or grants
    strictly less. The rule keeps its position when one was already there (ordering against the
    user's own rules is not this function's to change) and otherwise lands where the builder puts
    it: immediately before the catch-all, which is the only position where an exact-name rule can
    still match.

    A `dns` block that is not an object is left alone rather than replaced: `dns.servers` carries
    the encrypted resolver, and inventing a dns block here could silently drop it. The mapping is
    then simply not re-emitted — less access, never more.
    """
    outs = cfg.get("outbounds")
    if isinstance(outs, list):
        cfg["outbounds"] = [o for o in outs
                            if not (isinstance(o, dict) and o.get("tag") == DIRECT_LAN_TAG)]
    dns = cfg.get("dns")
    if isinstance(dns, dict):
        dns.pop("hosts", None)
    routing = cfg.get("routing")
    rules = routing.get("rules") if isinstance(routing, dict) else None
    at = None
    if isinstance(rules, list):
        keep = []
        for rule in rules:
            if isinstance(rule, dict) and rule.get("outboundTag") == DIRECT_LAN_TAG:
                at = len(keep) if at is None else at
                continue
            keep.append(rule)
        rules[:] = keep
    # Nothing to map, or nowhere safe to say it: everything above already removed what was there.
    if not hosts or not isinstance(dns, dict) or not isinstance(cfg.get("outbounds"), list):
        return
    dns["hosts"] = dict(hosts)
    cfg["outbounds"].append(rw_lan_outbound(settings))
    if isinstance(rules, list):
        rules.insert(at if at is not None else max(0, len(rules) - 1), rw_lan_rule(hosts))


def _rw_drop_routing_refs(cfg) -> None:
    """Stop routing rules naming an `rw-in` that is no longer emitted, in place.

    Cosmetic in xray (a rule matching a tag no inbound carries simply never fires) but not
    cosmetic HERE: this config has to pass `xray -test`, and a sanitize that will not validate
    is a revocation that does not happen. Leaving the reference out makes the result the same
    shape build_config emits with the feature off — a shape that validates on every apply —
    instead of one nothing else in the codebase ever produces.

    A rule whose ONLY inbound was `rw-in` is left exactly as it is rather than emptied or
    deleted: xray reads an empty `inboundTag` as "no inbound condition at all", so pruning the
    last entry would silently WIDEN the rule from one inbound to every inbound. An untouched
    rule that can never match is inert; a widened one is a routing change nobody asked for.
    """
    rules = cfg.get("routing", {}).get("rules") if isinstance(cfg.get("routing"), dict) else None
    if not isinstance(rules, list):
        return
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        tags = rule.get("inboundTag")
        if isinstance(tags, list) and RW_TAG in tags and any(t != RW_TAG for t in tags):
            rule["inboundTag"] = [t for t in tags if t != RW_TAG]


def _rw_write_config(state) -> bool:
    """Bring the live config into line with the store, so it no longer carries the revoked
    credential. Write ONLY — nothing is reloaded, nothing is started.

    Returns whether the file on disk PROVABLY grants no more than the store does: because this
    rewrote it, or because it already said exactly that (see the delta check below).

    This is the half of a revocation that is safe in every supervisor state, and separating it
    from the reload is what lets the revocation be complete without ever bringing xray up.
    ConfigManager.apply_irreversible() is already exactly this (validate → durably invalidate the
    rollback pairing → atomic write; the reload has always been the caller's separate step in
    apply_node too), so there is no second writer of config_path here — it stays the only one,
    which is the property that keeps the on-disk config and the rollback provenance consistent.

    `apply_irreversible` rather than `apply` because the ordinary path FILES the config it
    replaces as the undo target and marks the pairing valid, which for a revocation means
    publishing a promotable pre-revocation config for as long as it takes to invalidate it
    afterwards. Here the pairing is never written at all.

    Two ways to produce the clean config, tried in the order of what each can promise:

      1. Sanitize the file that is there. Needs no node and no re-render, so it is the only one
         available once the node has been deleted, and it changes nothing but the inbound.
      2. Rebuild from the node still on file. Needs a node, but it does not need the live config
         to be readable — so it is what repairs a missing or malformed file, and it is the
         second chance when a sanitized config will not validate.

    Either producing or writing a clean config can fail (an unreadable file with no node left to
    rebuild from, a full disk, an `xray -test` that rejects the result). It fails safe rather
    than raising: the file may still carry the credential, and the caller has to take that as a
    failed revocation — not as a 500, and not as a success.

    That promise covers the SETUP as well as the two producers. Constructing the ConfigManager
    and looking the rebuild node up used to sit outside the per-producer `try`, so a raise there
    left the function through the one exit its callers do not handle: `_rw_revoke_apply` runs
    inside the handler's DB transaction, so it took the client deletion down with it, and
    `_rw_guard_start` turned it into a 500 out of `/xray/start`. The node lookup failing is not
    even fatal — sanitizing the file on disk needs no node — so it degrades to None instead of
    ending the write.

    AND IT DOES NOT WRITE A FILE THAT WOULD NOT CHANGE. `apply_irreversible` durably invalidates
    the rollback provenance BEFORE it touches the config — that ordering is the whole crash
    argument for it, and it is right — so calling it on a config identical to the one already on
    disk destroys the operator's undo to achieve nothing. That is not hypothetical: the recovery
    path runs this on every retry, minutes or hours after an ordinary apply has published a
    perfectly good post-revocation target, and `_rw_drop_unsafe_rollback` then had nothing left to
    preserve — it examined a pairing this call had already invalidated and could only conclude, on
    every pass, that it was not safe.

    "Would not change" is MEASURED, never assumed: the config a producer just built is compared,
    by the same content digest the provenance pairing itself is keyed on, against the live file as
    it was read at the top of this function. Equal digests mean writing it would leave both the
    file and the pairing exactly as they are, so skipping the write is not a shortcut — it is the
    same end state reached without collateral damage. The answer is honest too: a config derived
    from the store that matches the file means the file grants no more than the store, which is the
    whole of what this function promises. Anything short of equal digests is written as before,
    because a file that differs by so much as a stale short id is a file that still grants
    something, and guessing wrong in that direction is the leak this machinery exists to close.
    """
    try:
        mgr = ConfigManager(state.settings, xray_bin=state.xray_bin)
    except Exception:
        logger.exception("a remote-access revocation could not open the config manager")
        return False
    try:
        node = _rw_rebuild_node(state)
    except Exception:
        logger.exception("a remote-access revocation could not read the node to rebuild from; "
                         "sanitizing the config on disk instead")
        node = None
    # ONE read, shared by the sanitize and the comparison, for the reason `_rw_sanitized_config`
    # gives: a second read would be a second file.
    on_disk = _rw_config_from_disk(state)

    def _sanitize():
        return _rw_sanitized_config(state, on_disk)

    def _rebuild():
        return None if node is None else build_node_config(node, state.settings, state.store)

    for produce in (_sanitize, _rebuild):
        try:
            cfg = produce()
            if cfg is None:
                continue
            if on_disk is not None and config_digest(cfg) == config_digest(on_disk):
                logger.info("a remote-access revocation left the live config alone: it already "
                            "grants no more than the store does")
                return True
            ok, _out = mgr.apply_irreversible(cfg)
        except Exception:
            continue
        if ok:
            return True
    return False


def _rw_live_excess(state) -> bool:
    """Whether the config ON DISK would hand out remote access the store does NOT grant.

    The question a bare `/xray/start` has to ask before serving that file. Every other way the
    config is served rebuilds it from the store first (`reapply_active_node` → `apply_node`), so
    it agrees with the store by construction; the bare start — no active node, so nothing to
    rebuild from — is a plain `supervisor.start()` on whatever the file happens to say. That is
    the door every leak on this path has eventually gone through: a revocation that could not
    write the file (both config producers rejected, a full disk, an `xray -test` failure) ends at
    the stop, and the stop is exactly what the next start undoes. Checking here rather than in
    the revocation covers all of them at once, including the ones not yet thought of — the file
    is untrusted input to the start, whatever left it in that state.

    SUBSET, not equality: a file listing fewer clients than the store grants is stale in the
    harmless direction (the next apply widens it); one listing a client, short id, server name,
    port or private key the store no longer grants is not. `rw_grants` derives both sides from
    the block definition, so a credential added to the inbound is a credential compared here.

    An unreadable or malformed file is NOT excess, deliberately — the opposite of the
    `_rw_live_inbound` rule, and for a reason that inverts with it. There, xray is already
    running on a config it loaded long ago and the file is not evidence about what is being
    served. Here the file IS what is about to be loaded, and xray will not come up on something
    it cannot parse — a config that cannot be served grants nothing, and refusing to start on it
    would only take away the operator's ability to see the process fail.
    """
    cfg = _rw_config_from_disk(state)
    return False if cfg is None else _rw_excess(state, cfg)


def _rw_excess(state, cfg: dict) -> bool:
    """`_rw_live_excess` for a config in hand — the rollback candidate, before it is promoted."""
    inbounds = cfg.get("inbounds") if isinstance(cfg, dict) else None
    if not isinstance(inbounds, list):
        return False
    live = frozenset().union(*[rw_grants(i) for i in inbounds
                               if isinstance(i, dict) and i.get("tag") == RW_TAG] or [frozenset()])
    if not live:
        return False
    try:
        rw = rw_mod.resolve(state.store)
    except ValueError:
        rw = None
    granted = rw_grants(rw_inbound_block(rw)) if rw is not None else frozenset()
    return bool(live - granted)


def _rw_reconcile_before_serving(state) -> bool:
    """Whether the config on disk may be served — after one attempt to bring it into line, which
    is all a revocation ever needed. False means it still grants revoked access and could not be
    rewritten, and nothing may load it.

    Fails CLOSED. Not starting costs the operator a tunnel they can restore by fixing whatever
    stopped the sanitize from landing; starting hands a lost device its access back, minutes
    after the panel said it was cut off. Between an outage and an unrevocation, an access-control
    path takes the outage.
    """
    if not _rw_live_excess(state):
        return True
    if _rw_write_config(state):
        conn_events.record(state.store, "rw-revoke",
                           "cleaned a remote-access grant the store no longer makes out of the "
                           "config before serving it")
        return True
    return False


def rw_start_guard(state) -> bool:
    """The credential guard as the SUPERVISOR's start hook (wired in `state.build_state`).

    The route-level calls below cover the two starts a request can ask for. They cannot cover the
    one nobody asks for: the liveness watchdog restarts a crashed xray with a plain
    `supervisor.start()`, on whatever config is on disk, with no request and no route in sight —
    so a revocation that could not rewrite the file (a full disk, a rejected `xray -test`) ends
    at the stop, and twenty seconds later the watchdog undoes the stop and serves the revoked
    credential again. Guarding the callers one at a time leaves that door and every future one
    open; the guard belongs where the process is actually spawned.

    Returns a bool and never raises: this runs inside the supervisor's own lock, on a background
    thread, where an exception would be swallowed by the watchdog's best-effort handler and read
    as "start attempted". False = do not spawn — which the supervisor reports as a failed start,
    so the watchdog backs off and the panel shows why instead of looping.
    """
    try:
        return _rw_reconcile_before_serving(state)
    except Exception:
        logger.exception("the remote-access start guard failed; refusing to start xray")
        return False


def _rw_guard_start(state) -> None:
    """`_rw_reconcile_before_serving` for a request, refusing with 409.

    The bare `/xray/start` — no active node, so nothing is rebuilt and the file is served as
    found — is what needs an answer with a status code on it. Called BEFORE anything is started,
    which is the only position where refusing still costs nothing. The other config the store did
    not produce goes through `_rw_guard_promotion` (a rollback candidate has to be reconciled
    before it is installed, not after), and the start nobody requests through `rw_start_guard`.
    """
    if not _rw_reconcile_before_serving(state):
        raise HTTPException(
            status_code=409,
            detail="refusing to serve this config: it still grants remote access that has been "
                   "revoked, and it could not be rewritten. Reconnect a node to rebuild it.")


def _rw_guard_promotion(state, mgr, candidate: dict) -> None:
    """Install `candidate` — the config `POST /rollback` wants to promote — only in a form the
    store actually grants. Raises 409 without touching the live config when that is impossible.

    The order is the whole point, and it was wrong: the candidate used to be promoted first and
    checked afterwards, so a sanitize that could not land left the credential-bearing file
    installed as the live config and returned 409 about it. Nothing rolled that back, and the
    file is what the next start comes up on — including the watchdog's, which asks no route for
    permission. "We refused" has to mean the file was never installed, not that we disapproved of
    installing it.

    A candidate that grants nothing the store does not is promoted by `rollback()` itself, which
    keeps the provenance pairing honest (a repeated rollback stays a provable no-op). One that
    does is rewritten and written through `apply_irreversible`, which validates it and publishes
    no pairing at all — a cleaned rollback must not leave a fresh target for a second attempt to
    promote back to the version we just refused.
    """
    if not _rw_excess(state, candidate):
        if not mgr.rollback():
            raise HTTPException(status_code=409, detail="no promotable rollback target")
        return
    clean = _rw_reconciled(state, candidate) if _rw_config_shape(candidate) else None
    written = False
    if clean is not None and not _rw_excess(state, clean):
        try:
            written = mgr.apply_irreversible(clean)[0]
        except Exception:
            logger.exception("the rollback candidate could not be rewritten")
            written = False
    if not written:
        raise HTTPException(
            status_code=409,
            detail="refusing to roll back: the last-good config still grants remote access that "
                   "has been revoked, and it could not be rewritten. Nothing was promoted.")
    conn_events.record(state.store, "rw-revoke",
                       "cleaned a remote-access grant the store no longer makes out of the "
                       "rollback target before promoting it")


def _rw_live_inbound(state) -> bool:
    """Whether the config xray is currently running on actually carries the remote-access
    inbound. The on-disk config IS what the supervisor loads, so it is the honest answer to
    "could a revoked client still get in".

    Anything we cannot read back — missing, truncated, unparseable — counts as serving.
    MISSING especially: xray keeps serving the configuration it already loaded long after the
    file is unlinked, so an absent file is evidence about the filesystem, not about what is
    listening. Treating it as "nothing is live" made _rw_revoke answer "not-live" and do
    nothing at all, which is the silent no-op the whole fail-safe path exists to prevent. The
    only proof that nothing is being served is a supervisor that is not running (see
    _rw_serving), and that is checked separately.

    The fail-safe covers the STRUCTURE, not just the read and the parse. A file that parses to
    a valid JSON value of the wrong shape — `[]`, a bare string, `inbounds` holding anything
    but a list of objects — used to raise out of here (`[]` → AttributeError), so a revocation
    500ed having neither rebuilt nor stopped anything: the same silent no-op arriving through
    malformed content instead of an unreadable file. Only a well-formed `inbounds` list with
    no `rw-in` tag POSITIVELY PROVES nothing is being served; every other shape — including an
    object with no `inbounds` at all, which xray would refuse to start on and so cannot be
    what a running xray loaded — says we failed to read the config, not that it is harmless.
    """
    try:
        with open(state.settings.config_path) as f:
            cfg = json.load(f)
        inbounds = cfg["inbounds"]
        if not isinstance(inbounds, list) or not all(isinstance(i, dict) for i in inbounds):
            return True
        return any(i.get("tag") == "rw-in" for i in inbounds)
    except Exception:
        return True


def _rw_supervisor_running(state) -> bool | None:
    """Tri-state: True = running, False = affirmatively not running, None = we could not tell.

    The three answers are not interchangeable, and collapsing them is how this went wrong twice
    in opposite directions. Reading "unknown" as not-running let a revocation return `not-live`
    having done nothing; reading it as running (correct for the fail-safe REPORTING in
    _rw_serving) lets the revocation logic reach a rebuild, and a rebuild restarts xray. Callers
    that ACT on the answer must therefore branch on the third value, not on truthiness.
    """
    try:
        return bool(state.supervisor.status().get("running"))
    except Exception:
        return None


def _rw_serving(state) -> bool:
    """Whether remote access is being served RIGHT NOW — the value the screen reports as `live`.

    NOT derived from `active_node_id`. `disconnect` clears that id and deliberately leaves xray
    running on the config it already loaded, so the inbound keeps accepting clients with no
    active node: reading the id there reports "not live" about an inbound that is serving, and
    reports the same about a successful revocation rebuild, which is the inverse of the bug it
    was meant to describe. Supervisor state plus the config the supervisor actually loaded is
    the only honest answer. An unreadable config counts as serving, for the same fail-safe
    reason _rw_live_inbound does: we cannot prove nothing is listening.

    A supervisor that cannot be QUERIED counts as serving for that same reason. Only a
    supervisor that affirmatively reports not-running is proof that nothing is served; an
    exception is a failure to observe, not an observation of absence, and reading it as "not
    live" let a revocation issued while the supervisor is unreachable return having done
    nothing — the same silent no-op a missing config used to cause, through another door.
    """
    return _rw_supervisor_running(state) is not False and _rw_live_inbound(state)


# The outcomes that PROVE the runtime no longer serves what the store stopped granting: the
# config was rebuilt and reloaded, or rewritten while xray was down, or provably never carried
# the inbound, or xray was confirmed stopped. Only these clear the marker. Anything else — an
# outcome that could not confirm the stop, or no outcome at all because the process died
# mid-revocation — leaves it set, and the recovery below retries.
_RW_RECONCILED = frozenset({"reapplied", "rebuilt", "cleaned", "not-live", "stopped"})


def _rw_mark_reconcile_pending(store) -> None:
    """Record, IN THE CALLER'S TRANSACTION, that a narrowing mutation has not yet been pushed
    into the running xray.

    A revocation commits its store half before it touches the runtime — it has to, because the
    runtime work opens transactions of its own and a failure in one used to discard the whole
    unit (see `_rw_revoke`). That ordering is right and it leaves a window: between the commit
    and the reconcile, the store grants less than the config an ALREADY-RUNNING xray is serving.
    Nothing else closes that window. `apply_lock` serializes requests, not process death. The
    supervisor's start guard only gates a spawn, and the running process needs none. The liveness
    watchdog only acts on an xray in 'error' state, and a healthy process serving a revoked
    credential looks exactly like a healthy process.

    So the fact that the runtime is behind is made DURABLE, atomically with the mutation that put
    it behind. Deliberately NOT guarded: it goes in with the narrowing or the narrowing does not
    go in at all. A commit that silently dropped the marker would be the original hole with no
    record that it exists.
    """
    store.set_setting(RW_PENDING_KEY, "1")


def _rw_reconcile_is_pending(state) -> bool:
    """`rw_reconcile_is_pending` for a `state` — see the controller for the read's failure rule."""
    return rw_reconcile_is_pending(state.store)


def _rw_clear_reconcile_pending(state) -> bool:
    """`rw_clear_reconcile_pending` for a `state`.

    Both live in the controller now, next to the `apply_node` success that is the other thing
    entitled to clear this marker (see `_rw_complete_pending`). One implementation, so the read-back
    that makes "cleared" mean cleared cannot drift between the two callers of it.
    """
    return rw_clear_reconcile_pending(state.store)


def rw_reconcile_pending(state) -> bool:
    """Finish a revocation whose store half committed and whose runtime half did not. Returns
    whether nothing is pending any more.

    Wired into BOTH ends of the panel's own lifecycle — the lifespan startup (`app.py`) and every
    liveness tick (`health/liveness.py`) — and neither asks whether xray is running first. That
    is the entire point: the case this exists for is a HEALTHY process still serving a credential
    the store gave up. The start guard cannot see it (nothing is spawning), the watchdog will not
    touch it (`state()` reads 'working'), and no request is coming.

    The reconcile is NOT the revocation again. Re-running `_rw_revoke` was the obvious thing and
    it was wrong, because it is far more than the runtime half: an irreversible rebuild that
    restarts xray and rewrites the session bookkeeping, an unconditional drop of the rollback
    target, and an incident event per attempt. Retried on a schedule those become damage of their
    own — an ordinary apply that ran after the marker was set publishes a legitimate undo target,
    and the next retry destroyed it; the connection log filled with one entry per attempt of an
    episode that is reported once by design. What is actually outstanding is narrower and is all
    `_rw_reconcile_runtime` does: make the config on disk stop granting what the store stopped
    granting, and make sure nothing is still serving an older one.

    IT CANNOT WEDGE THE PANEL, by construction:
      * No request handler reads the marker. A marker that never clears refuses nothing, blocks
        nothing, and changes no response.
      * It never STARTS xray. It reloads only through `reload_if_running`, which decides under the
        supervisor's own lock, so a stuck marker cannot become a restart loop — the failure mode a
        naive "pending → restart" recovery would have.
      * The caller paces the retries (the liveness loop backs off to 10 minutes) and reports the
        episode once. The retries themselves file no events at all, so a permanently stuck marker
        cannot flush the connection-event ring.
      * Startup logs and continues. A panel that refused to boot on an unreconcilable marker
        would take away the one screen the operator could fix it from.

    Never raises: it runs on a background thread and in the lifespan, where an exception is
    either swallowed or takes the boot down.
    """
    if not _rw_reconcile_is_pending(state):
        return True
    try:
        with apply_lock:                     # RLock: nesting under a caller that holds it is fine
            # Re-check under the lock — a request may have finished the same revocation between
            # the read above and here, and re-running it would rebuild the config for nothing.
            if not _rw_reconcile_is_pending(state):
                return True
            how, cleared = _rw_reconcile_runtime(state)
    except Exception:
        logger.exception("a pending remote-access reconciliation could not be finished")
        return False
    # BOTH halves, because the caller treats True as "this episode is over": it resets the
    # backoff and the once-per-episode event flag on that word. Reporting success off the outcome
    # alone while the marker was still set — a settings write that failed — sent the next tick
    # through the whole reconciliation again, immediately, and again 20 s later, reloading the
    # live tunnel every time.
    resolved = how in _RW_RECONCILED and cleared
    logger.warning("a remote-access revocation had not reached the running xray; reconciling it "
                   "%s (%s%s)", "completed" if resolved else "is still pending", how or
                   "no outcome", "" if cleared else "; the marker could not be cleared")
    return resolved


def _rw_reconcile_runtime(state) -> tuple[str, bool]:
    """The RECOVERY-specific revocation. Returns `(how, marker provably cleared)`.

    A retry is not the request it is finishing, and treating it as one — calling `_rw_revoke`
    again — did three things that get worse the more often they happen:

      * IT REAPPLIED THE ACTIVE NODE. That restarts xray through a full apply and rewrites the
        session bookkeeping `apply_node` owns: the uptime anchor, the traffic baselines the
        Dashboard's "this session" is measured from, the previous-node record a rollback reads.
        None of it is what a revocation is for, and a retry every 20 s resets all of it. The
        write-only path reaches the same file (`_rw_write_config` sanitizes the live config or
        rebuilds it from the node on record, through `apply_irreversible`), needs no node to be
        connected, and touches neither the net rules nor a single stored counter.
      * IT DROPPED THE ROLLBACK TARGET UNCONDITIONALLY. Right for the request — the snapshot on
        file is by construction from before that revocation — and false for every retry after it:
        an ordinary apply may have run in between and filed a perfectly good undo target, which
        the next retry then destroyed. So the retry ASKS instead of assuming (see
        `_rw_drop_unsafe_rollback`), and fails closed on any answer it cannot get.
      * IT FILED AN INCIDENT EVENT PER ATTEMPT. The liveness loop reports the episode exactly
        once precisely so a stuck marker cannot flush the 40-entry connection log; the reconciler
        underneath it was recording two more on every pass. The retries are silent in the log now
        and loud in the ordinary one.

    What it deliberately keeps is the fail-safe: an outcome that cannot prove the credential is
    gone leaves the marker set, and the marker is cleared only after it is read back absent.
    """
    how = _rw_push_to_runtime(state, _rw_supervisor_running(state), quiet=True)
    _rw_drop_unsafe_rollback(state)
    if how not in _RW_RECONCILED:
        return how, False
    return how, _rw_clear_reconcile_pending(state)


def _rw_drop_unsafe_rollback(state) -> None:
    """Invalidate the rollback pairing when — and only when — the config it points at would hand
    back access the store no longer grants.

    `_rw_revoke` drops it unconditionally and should: at that moment the snapshot on file is
    necessarily a config from before the revocation, so whether it still grants what was just
    revoked is exactly the question a rollback would answer wrongly. A RETRY cannot say that. It
    may be running minutes or hours later, after an ordinary apply has published a fresh pairing
    that agrees with the store, and dropping that on every pass takes the operator's undo away
    for as long as the marker is stuck — a cost with nothing on the other side of it.

    So the question is asked. A candidate that grants nothing the store does not is left alone;
    anything else — including a target we cannot read, parse or compare — is invalidated, because
    "we could not tell" is not permission to keep a promotable pre-revocation config around.

    Asking it is only worth anything if there is still something to ask ABOUT, and for a while
    there was not: the runtime push runs first, and when it writes it writes through
    `apply_irreversible`, which durably invalidates the pairing before touching the file. That is
    correct for a write — a config produced by a revocation may not become an undo target — but it
    used to happen even when the file already agreed with the store, so the recovery destroyed the
    target it was about to try to preserve and this function could only ever find nothing. The
    write is skipped on a proven no-op now (see `_rw_write_config`), which is exactly the case this
    function exists for: nothing to write, and a pairing published after the marker that deserves
    to survive. When there IS something to write, the pairing is gone by design and there is
    nothing here to keep.

    Never raises and never records an event: this runs on the recovery path, where the episode is
    reported once by the caller and everything else belongs in the log.
    """
    try:
        mgr = ConfigManager(state.settings, xray_bin=state.xray_bin)
    except Exception:
        logger.exception("a pending remote-access reconciliation could not open the config "
                         "manager to check the rollback target")
        return
    try:
        candidate = mgr.rollback_target()
        safe = candidate is not None and not _rw_excess(state, candidate)
    except Exception:
        logger.exception("a pending remote-access reconciliation could not read the rollback "
                         "target; dropping it")
        safe = False
    if safe:
        return
    if not mgr.invalidate_rollback():
        logger.error("a pending remote-access reconciliation could not invalidate the rollback "
                     "target; POST /rollback may still be able to reinstate the revoked "
                     "credential")


def _rw_revoke(state) -> str:
    """Push a REVOCATION into the running xray, failing safe. Returns HOW it was applied.

    CALLED WITH THE CREDENTIAL CHANGE ALREADY COMMITTED, and with `apply_lock` still held. The
    handlers used to run this inside their own `store.transaction()`, on the theory that a
    revocation is one unit; it cannot be. Every runtime step here can open a transaction of its
    own — `apply_node` persists the active-node bookkeeping in one — and `_SafeConn` nests by
    DEPTH: a failure in an inner block marks the whole unit rollback-only, so the outer exit
    discarded the credential removal and the incident event no matter how carefully the call that
    contained it was guarded. The operator got a 500, the device stayed granted in the store, and
    xray had often already been stopped. A revocation that fails open is not a revocation.

    So the store is committed first and the runtime work happens after. `apply_lock` is what
    keeps that pair atomic against other requests — every other mutator of the config, the
    supervisor and the net rules takes it too — and it is the only thing that ever could: a DB
    transaction cannot roll back a stopped process or a rewritten config file, which is precisely
    the state a failed revocation leaves behind.

    The window this opens is "committed, not yet applied", and it is the safe direction: the
    store already grants less than the config, and every path that can make that config live
    reconciles it against the store first — an apply/connect rebuilds from the store, and the
    two that serve a file as found (`/xray/start`, `POST /rollback`) plus the supervisor's own
    start guard sanitize or refuse. A crash inside the window therefore ends with the credential
    gone and the file cleaned by whoever next tries to serve it, never with it quietly restored.

    THAT COVERS EVERY WAY THE FILE BECOMES LIVE, AND ONE WAY NEEDS NO SPAWN AT ALL: the xray that
    is already up. It loaded the pre-revocation config minutes ago and keeps serving it; the
    start guard gates `Popen` and nothing else, and the watchdog only touches a supervisor in
    'error' state, so a healthy process holding a revoked credential is precisely what every
    guard above is blind to. `apply_lock` does not help either — it excludes competing requests,
    not the thread or the process dying mid-revocation.

    So the caller records the gap durably, in the same transaction as the mutation that opened it
    (`_rw_mark_reconcile_pending`), and this function clears it only when the runtime work
    actually reached one of the outcomes that PROVE the credential is no longer served
    (`_RW_RECONCILED`). Startup and every liveness tick finish whatever is still marked, running
    xray or not — see `rw_reconcile_pending`.

    A rollback may not undo it, so the rollback target is dropped on the way out — see
    `_rw_revoke_apply` for the mechanics and `ConfigManager.invalidate_rollback` for why the
    provenance marker is the right lever. Unconditional, including the paths that write nothing:
    the snapshot on file is by construction a config from BEFORE this revocation, so whether it
    still grants what was just revoked is exactly the question a rollback would answer wrongly.
    `finally`, because a revocation that ends in a 502 is a revocation whose outcome is unknown
    — the last state in which restoring an older config unexamined is a good idea.

    NO path that WRITES depends on this sweep any more. Both of them — `_rw_write_config` and
    the connected rebuild — go through `apply_irreversible`, which never publishes a pairing to
    begin with and refuses to touch the live config if it cannot durably say so. What is left
    for the sweep is the paths that write NOTHING (`not-live`, `stopped`, a revocation that
    ended in an exception): there the live config is untouched, so the marker still pairs it
    with a snapshot from before the revocation, and that snapshot grants what was just revoked.
    A failure there IS reportable — a pre-revocation snapshot may still be promotable — so it is
    logged as an error and written into the connection log rather than swallowed.
    """
    try:
        how = _rw_revoke_apply(state)
    finally:
        if not ConfigManager(state.settings, xray_bin=state.xray_bin).invalidate_rollback():
            logger.error("a remote-access revocation could not invalidate the rollback target; "
                         "POST /rollback may still be able to reinstate the revoked credential")
            conn_events.record(
                state.store, "rw-revoke",
                "could not drop the rollback target after a remote-access revocation — "
                "rolling back may reinstate the revoked device")
    # AFTER the sweep and outside its `finally`, so an outcome that never arrived — the runtime
    # half raised, or the process died before this line — leaves the marker exactly where the
    # transaction put it and the recovery picks it up.
    if how in _RW_RECONCILED:
        _rw_clear_reconcile_pending(state)
    return how


def _rw_revoke_apply(state) -> str:
    """The revocation itself, ONCE. Call `_rw_revoke`, never this — on its own it leaves a
    rollback target that puts the revoked credential back — and never as a retry: the reconnecting
    rebuild below belongs to the request that asked for it, and `_rw_reconcile_runtime` is what a
    later attempt runs instead.

    The lost-device path. `disconnect` deliberately leaves xray running on the old config and
    clears `active_node_id`, so reapply_active_node has nothing to rebuild and returns None —
    which used to mean the revoked uuid kept LAN + tunnel access until some unrelated rebuild
    happened. That is not an acceptable outcome for an access-control action, so when the
    normal reapply is unavailable this rewrites the live config WITHOUT reconnecting (config +
    xray reload only; the net rules stay exactly as disconnect left them), and if even that is
    impossible it stops xray. Never returns quietly having done nothing while xray still serves
    the client.

    THE FILE AND THE PROCESS ARE TWO SEPARATE QUESTIONS, and answering them together is how
    this path leaked in both directions at once:

      * Rewriting the on-disk config is safe in EVERY supervisor state — it neither starts nor
        reloads anything — and it is the only thing that keeps a revoked credential from coming
        back. The `stopped` and `not-live` branches used to leave the file alone, so the config
        still listed the revoked client; `/xray/start` with no active node is a bare
        supervisor.start() on that exact file, and the revoked credential was live again. A
        revocation that survives only until the next start is not a revocation.
      * Making the RUNNING process pick that file up is a reload, and reload() is an
        unconditional stop→start. Only a supervisor we affirmatively know is running may be
        told to do it — and "know" cannot mean the sample taken at the top of this function.
        That sample and the reload are two separate holds of the supervisor's lock, so a child
        that exits in between turns the reload into a plain START of an xray that was down, on
        the config that happens to be on disk. The reload therefore goes through
        `reload_if_running`, which re-checks and restarts under one lock hold and reports which
        of the two it did. The same goes for reapply_active_node, which starts xray AND
        re-applies the net rules: `/xray/stop` keeps the node selection, so a revocation issued
        while xray was deliberately down used to bring the whole tunnel back UP in order to
        revoke.

    A revocation may take access away. It may never give any back — least of all by starting a
    process the operator stopped.

    And it has exactly one exit that means "done": a branch that reports HOW. A rebuild that
    fails is not one — it falls through to the write-only path and, failing that, to the stop,
    because the alternatives are reporting a revocation that did not happen or raising out of
    the transaction the whole handler runs in and rolling the revocation itself back.

    IT HAS NO OTHER EXIT AT ALL. Every call here is guarded, because the caller removed the
    credential from the store BEFORE calling: an exception escaping this function is a revocation
    whose runtime half stopped halfway with nothing to say for itself. It no longer un-commits
    the deletion (the transaction closes before this runs — see `_rw_revoke`), and that is
    exactly why the guards stay: the store is already the truth, so the operator must be told
    what the box did to match it, not handed a 500 that describes neither. A supervisor that
    throws on stop, a network backend that throws instead of reporting, a config manager that
    cannot even be constructed: all of them are recorded and returned, never raised. The
    connection log carries the detail of a failed stop; the OUTCOME carries the fact of it —
    `stopped` only when xray is confirmed down, `stop-failed` when the fail-safe stop ran and
    could not be confirmed, which is the case where the revoked credential may still be live.
    """
    running = _rw_supervisor_running(state)

    # Reconnecting is only ever right for a process we KNOW is up (see above); with xray down
    # or unobservable the rebuild-without-reconnecting path below writes the same file without
    # touching the process. Note this is what makes the write-only path load-bearing rather
    # than belt-and-braces: it is now the ONLY thing that happens in those states.
    if running is True:
        # IRREVERSIBLE, for the same reason `_rw_write_config` is: this rebuild reaches the live
        # config through the ordinary `apply()` otherwise, which files the config it replaces as
        # the undo target and marks the pairing valid — so the connected case published a
        # promotable PRE-revocation config, and the only thing that took it away was a sweep
        # running afterwards, in a `finally`, whose failure was logged and then reported as a
        # successful revocation. The pairing is now never written on this path either, and the
        # sweep is a backstop instead of the guard.
        try:
            res = reapply_active_node(state, irreversible=True)
        except Exception as exc:
            # It documents that it never raises, and every runtime call inside it is guarded —
            # but this is the one call site where being wrong about that costs the revocation
            # itself rather than a boot, so the claim is not taken on trust. A rebuild that
            # raised is a rebuild that failed: same fall-through, same event.
            logger.exception("a remote-access revocation's rebuild raised")
            res = ApplyResult(ok=False, error=f"rebuild raised: {exc}")
        if res is not None and res.ok:
            return "reapplied"
        if res is not None:
            # A failed REBUILD is not a finished revocation, and it may not be reported as one —
            # nor as a 502. Raising out of here abandons the runtime half of a revocation whose
            # store half is already committed: the operator is left a 500-shaped error about a
            # device the store no longer grants but xray may still be serving. Everything below
            # this point is already the answer to "no clean config could be produced or made
            # live" — sanitize the file instead of re-rendering it, and take xray down if even
            # that fails — so a failed rebuild continues down it rather than ending here.
            logger.error("a remote-access revocation could not rebuild the active node's config "
                         "(%s); falling back to rewriting the live config", res.error)
            conn_events.record(
                # Truncated: an apply error can carry the whole of `xray -test`'s output, and
                # the event ring lives in one settings value that the backup document has to
                # stay under. The full text is in the log line above.
                state.store, "rw-revoke",
                f"rebuilding the active node's config failed ({(res.error or '')[:200]}) — "
                "falling back to rewriting the live config")
            # Re-observe the supervisor: `apply_node` fails CLOSED and may have stopped xray on
            # its way out, and the branches below must not tell a stopped process to reload
            # (a reload is a start) nor call a config nothing is serving "live".
            running = _rw_supervisor_running(state)

    return _rw_push_to_runtime(state, running)


def _rw_reload_if_running(state) -> bool | None:
    """`supervisor.reload_if_running()`, guarded. True = it was running and came back up ready;
    None = it was not running and NOTHING was started; False = anything else, including a
    supervisor that threw on the way, which is a failed apply and never a reason to abandon a
    revocation whose store half is already committed."""
    try:
        return state.supervisor.reload_if_running()
    except Exception:
        logger.exception("a remote-access revocation's reload raised")
        return False


def _rw_push_to_runtime(state, running: bool | None, quiet: bool = False) -> str:
    """Make the runtime stop serving what the store stopped granting, and report HOW.

    The whole of what a revocation owes the running system, and therefore the whole of what a
    later retry has to repeat: everything above this — the reconnecting rebuild, the rollback
    provenance, the session bookkeeping — belongs to the REQUEST and is done once (see
    `_rw_reconcile_runtime` for why repeating it is destructive).

    `quiet` suppresses the connection-log entry, not the logging: on the recovery path the
    episode is recorded once by the caller, and one entry per attempt would flush the 40-entry
    ring that is the only record of what the box did with a lost device.
    """
    # Read before anything is written: the question is what the config the supervisor LOADED
    # carries, and the write below is precisely what stops the answer being true. A config that
    # provably holds no rw-in cannot hold the revoked credential either, so there is nothing to
    # cut and nothing to clean.
    #
    # THE FILE IS NOT THE PROCESS, THOUGH, AND THIS IS WHERE THAT BIT. The clean file is exactly
    # what an irreversible reapply leaves behind when it WROTE the new config and then could not
    # make the old process load it — a reload whose stop failed, an xray that outlived SIGKILL —
    # and it is also what turning the feature off or removing the last enabled client produces,
    # because the rebuilt config then carries no `rw-in` at all. Reading the file and answering
    # `not-live` there told the operator "nothing was live to cut" about a process that is still
    # up and still serving every credential it loaded minutes ago, and cleared the durable marker
    # on the way out, so nothing would ever come back to finish it.
    #
    # `not-live` therefore needs the supervisor to have been AFFIRMATIVELY observed down. With a
    # process that is running, or one we cannot query, the file proves nothing about what is being
    # served and only an affirmed reload of it — or an affirmed stop — ends this revocation.
    if not _rw_live_inbound(state):
        if running is False:
            # The one exit that changed nothing because there was nothing to change. It used to
            # be shared with the `cleaned` branch below, which rewrites the config and is a
            # completed revocation; one string cannot carry both.
            return "not-live"
        if running is True:
            reloaded = _rw_reload_if_running(state)
            if reloaded is True:
                # It is now demonstrably serving the file we just read, which carries no inbound
                # at all — the same proof `rebuilt` reports on the writing path.
                return "rebuilt"
            if reloaded is None:
                # It was not running after all, and nothing was started to find that out.
                return "not-live"
        return _rw_fail_safe_stop(state, running, quiet=quiet)

    # "The file grants no more than the store", however that came to be true — a rewrite, or a file
    # that already agreed and was therefore left alone rather than rewritten over itself (see
    # `_rw_write_config`). Every branch below asks only that question of it.
    clean = _rw_write_config(state)

    if running is True and clean:
        # `reload_if_running`, not `reload`: `running` was sampled before this and the
        # supervisor's lock has been released and retaken several times since. A child that
        # exited in between would be STARTED by a bare reload — on the config it happens to find
        # — which is the one thing a revocation may never do.
        reloaded = _rw_reload_if_running(state)
        if reloaded is True:
            return "rebuilt"
        if reloaded is None:
            # It was not running after all. The file it would come up on no longer names the
            # revoked client, so this is the same completed revocation the affirmatively-down
            # branch below reports — reached through a process that went away rather than one the
            # operator had already stopped.
            running = False
    if running is False and clean:
        # Affirmatively down, and now the file it would come up on no longer names the revoked
        # client. Nothing is being served and nothing will be on a later start — which is the
        # whole of what this revocation had to achieve. Starting xray to "apply" it would be
        # the one outcome worse than doing nothing, and stopping an already-stopped process
        # proves nothing.
        #
        # `cleaned`, NOT `not-live`. The config DID carry the inbound (checked above) and does not
        # carry the revoked credential now; that is a completed, durable revocation, and
        # reporting it with the string that means "there was nothing to cut" told the operator
        # the opposite of what happened — the one message on this screen that has to be right,
        # because the next thing they decide is whether the lost device is still a problem.
        return "cleaned"

    return _rw_fail_safe_stop(state, running, quiet=quiet)


def _rw_fail_safe_stop(state, running: bool | None, quiet: bool = False) -> str:
    """The last branch: no clean config could be produced or made live, or the supervisor's state
    is unknown. Stop xray — remote access is gone for everyone, which is the safe direction; the
    alternative is a revoked device keeping its access — and bring the net rules to their
    tunnel-down state as /xray/stop does. Stopping is correct if xray was running and a no-op if
    it was not, so it is the one action that is safe under both readings.

    Both calls are guarded, and the reason is not tidiness. `supervisor.stop()` shells out to
    terminate/wait/kill; `stop_net` runs host commands bounded by a timeout that surface as an
    ordinary command failure. Either can raise, and by this point the credential is already out
    of the store — so a raise here would abandon the revocation's runtime half with nothing to
    say for itself.
    """
    stop_detail = ""
    try:
        # A bounded stop can report failure by RETURNING False (a child that outlived SIGKILL) as
        # well as by raising. Both mean the same thing here — xray may still be serving the
        # credential — and both have to be said out loud rather than folded into "stopped xray".
        # What neither may become is a restart: the process is still up and still holds the port,
        # so there is nothing to start and nothing that starting fixes.
        if state.supervisor.stop() is False:
            stop_detail = " (the stop itself failed: xray survived SIGKILL)"
            logger.error("a remote-access revocation could not stop xray: it survived SIGKILL")
    except Exception as exc:
        stop_detail = f" (the stop itself failed: {exc})"
        logger.error("a remote-access revocation could not stop xray: %s", exc)
    try:
        guard = stop_net(state.settings, state.net, state.store)
        net_detail = "" if guard.ok else f" (network stop failed: {guard.error})"
    except Exception as exc:
        # stop_net reports failure by RETURNING a NetResult; a raise means a backend that does
        # not honour that contract. Still telemetry, still not grounds to un-revoke.
        net_detail = f" (network stop raised: {exc})"
        logger.error("a remote-access revocation's network stop raised: %s", exc)
    # Say what actually happened. `stop()` on an xray that was already down changes nothing, and
    # recording it as "stopped xray" writes an action into the incident record that was never
    # taken — the one record an operator reads afterwards to work out what the box did with a
    # lost device. The three supervisor states earned three different outcomes; the event says
    # which, and does not claim observation it did not have — including the fourth outcome, a
    # stop that was attempted and did not happen.
    did = ("tried and failed to stop xray" if stop_detail else
           {True: "stopped xray",
            False: "ensured xray remained stopped",
            None: "issued a stop to an xray whose state could not be observed"}[running])
    logger.warning("a remote-access revocation %s%s%s", did, stop_detail, net_detail)
    if not quiet:
        conn_events.record(state.store, "rw-revoke",
                           f"{did} to apply a remote-access revocation{stop_detail}{net_detail}")
    # The RETURNED value has to carry that distinction too, and it did not. Everything above
    # already knew the stop had failed — the log line said so, the event said so — and then the
    # answer handed back was `stopped`, so the screen told the operator remote access was down
    # while the credential they had just revoked may still be listening. `stop_net` does not
    # close that gap: it governs forwarded client-to-WAN traffic, and the remote-access inbound
    # accepts connections TO the gateway. Nothing here proves that listener is gone.
    #
    # `stop-failed` is therefore its own outcome, and deliberately NOT in `_RW_RECONCILED`: the
    # marker stays set and the liveness recovery keeps retrying until xray is confirmed stopped
    # or rebuilt on a clean config.
    return "stopped" if not stop_detail else "stop-failed"


@router.get("/rw", response_model=RwOut)
def get_rw(request: Request, _: None = Depends(require_auth)) -> RwOut:
    return _rw_out(get_state(request))


@router.put("/rw", response_model=RwOut)
def put_rw(body: RwIn, request: Request,
           _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> RwOut:
    state = get_state(request)
    try:
        hosts = rw_mod.validate_hosts(body.hosts)
        nets = rw_mod.validate_nets(rw_mod.parse_nets(body.routed_nets))
        short_ids = rw_mod.validate_short_ids(rw_mod.parse_csv(body.short_ids))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if body.enabled:
        # Everything the inbound and the client artifacts need, checked at the boundary. Without
        # this you can arm an inbound that either never starts (empty shortIds) or starts with
        # nothing issuable to a client — both discovered much later and much less clearly.
        if not (body.private_key.strip() or
                (state.store.get_setting("rw_private_key") or "").strip()):
            raise HTTPException(status_code=422,
                                detail="set the Reality private key before enabling the inbound "
                                       "(generate one with `xray x25519`)")
        for value, what in ((short_ids, "at least one short id"),
                            (body.public_key.strip(), "the Reality public key"),
                            (body.endpoint.strip(), "the external endpoint"),
                            (rw_mod.parse_csv(body.server_names), "at least one server name")):
            if not value:
                raise HTTPException(status_code=422,
                                    detail=f"set {what} before enabling the inbound")
    # Any save that takes a credential away — the feature switched off, the private key rotated,
    # a short id rotated or removed, a server name removed, the port moved — revokes issued
    # clients just as surely as deleting them one by one, so it takes the fail-safe path rather
    # than the "stored, applied later" one. Decided by comparing the stored credential surface
    # before and after the write (see _rw_narrows).
    #
    # The `before` snapshot is taken INSIDE apply_lock, not on arrival. Two saves that overlap
    # serialize their writes here but would otherwise both classify against the surface they saw
    # on arrival, so the second one compares against credentials another request has already
    # replaced — and gets the answer wrong in both directions: a save that only widens reads as
    # a revocation (cutting every device off), and a save that drops the short id the other
    # request had just installed reads as a widening, leaving it live on the running inbound.
    # Classification only means anything against what is live at decision time.
    #
    # The transaction ends BEFORE the revocation runs (see `_rw_revoke`), and `apply_lock` is
    # what keeps the pair atomic against other requests — it is held across both halves, and no
    # other mutator of the live config or the supervisor can run without it.
    with apply_lock:
        with state.store.transaction():
            before = _rw_credentials(state.store)
            s = state.store.set_setting
            s("rw_enabled", "1" if body.enabled else "0")
            s("rw_port", str(body.port))
            s("rw_dest", body.dest.strip())
            s("rw_server_names", ",".join(rw_mod.parse_csv(body.server_names)))
            s("rw_short_ids", ",".join(short_ids))
            s("rw_public_key", body.public_key.strip())
            s("rw_endpoint", body.endpoint.strip())
            s("rw_hosts", json.dumps(hosts))
            s("rw_routed_nets", ",".join(nets))
            # "" means keep — the UI never receives the key, so a plain round-trip must not
            # blank it.
            if body.private_key.strip():
                s("rw_private_key", body.private_key.strip())
            narrows = _rw_narrows(before, _rw_credentials(state.store))
            if narrows:
                # Committed WITH the narrowing: from here on the store grants less than the
                # config the running xray loaded, and that gap has to outlive this process.
                _rw_mark_reconcile_pending(state.store)
            else:
                # A widening save is the opposite case: nothing has been taken away yet, so an
                # apply that fails SHOULD take the stored settings back down with it, and this
                # stays inside the transaction to do exactly that.
                _rw_apply(state)
        how = _rw_revoke(state) if narrows else ""
    return _rw_out(state, revocation=how)


@router.post("/rw/clients", response_model=RwOut, status_code=201)
def add_rw_client(body: RwClientIn, request: Request,
                  _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> RwOut:
    state = get_state(request)
    with apply_lock, state.store.transaction():
        try:
            rw_mod.add_client(state.store, body.email)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        _rw_apply(state)
    return _rw_out(state)


@router.patch("/rw/clients/{client_id}", response_model=RwOut)
def patch_rw_client(client_id: str, body: RwClientPatch, request: Request,
                    _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> RwOut:
    """Suspend or resume one client. Suspending the last enabled one drops the whole inbound
    (xray will not start on an empty client list) — that is the documented behaviour, not a bug."""
    state = get_state(request)
    with apply_lock:
        with state.store.transaction():
            if not rw_mod.set_client_enabled(state.store, client_id, body.enabled):
                raise HTTPException(status_code=404, detail="client not found")
            # Suspending is a revocation; resuming only grants, so it can wait for the next
            # apply — and an apply that fails may still take a mere grant back down with it.
            if body.enabled:
                _rw_apply(state)
            else:
                _rw_mark_reconcile_pending(state.store)   # committed with the suspension
        how = "" if body.enabled else _rw_revoke(state)
    return _rw_out(state, revocation=how)


@router.post("/rw/short-id", response_model=RwShortIdOut)
def rw_new_short_id(request: Request,
                    _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> RwShortIdOut:
    """A fresh Reality short id for the operator to paste. Not persisted here — it only lands
    in the config once it is saved with the rest of the settings."""
    return RwShortIdOut(short_id=rw_mod.gen_short_id())


@router.delete("/rw/clients/{client_id}", response_model=RwOut)
def delete_rw_client(client_id: str, request: Request,
                     _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> RwOut:
    state = get_state(request)
    with apply_lock:
        with state.store.transaction():
            if not rw_mod.delete_client(state.store, client_id):
                raise HTTPException(status_code=404, detail="client not found")
            _rw_mark_reconcile_pending(state.store)       # committed with the deletion
        how = _rw_revoke(state)          # committed above; see `_rw_revoke` for why the order
    return _rw_out(state, revocation=how)


def _rw_client(state, client_id: str) -> dict:
    for c in rw_mod.get_clients(state.store):
        if c["id"] == client_id:
            return c
    raise HTTPException(status_code=404, detail="client not found")


@router.get("/rw/clients/{client_id}/link", response_model=RwLinkOut)
def rw_client_link(client_id: str, request: Request,
                   _: None = Depends(require_auth)) -> RwLinkOut:
    state = get_state(request)
    try:
        return RwLinkOut(link=rw_mod.link(state.store, _rw_client(state, client_id)))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/rw/clients/{client_id}/config", response_model=RwConfigOut)
def rw_client_config(client_id: str, request: Request,
                     _: None = Depends(require_auth)) -> RwConfigOut:
    """Shadowrocket .conf for one client. Served under auth and downloaded from inside the
    network on purpose — no public subscription URL, since exposing the panel to the internet
    to serve one config file would be a far bigger hole than the inbound it configures."""
    state = get_state(request)
    client = _rw_client(state, client_id)
    try:
        conf = rw_mod.shadowrocket_conf(state.store, client, _rw_nets(state))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return RwConfigOut(filename=f"{client['email']}.conf", config=conf)
