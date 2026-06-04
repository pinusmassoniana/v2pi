import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Request, HTTPException
from pi_gw_panel.api.schemas import (
    LoginIn, SetupIn, PasswordChangeIn, NodeIn, NodeOut, NodeUpdate, StatusOut,
    SubscriptionIn, SubscriptionPatch, SubscriptionOut,
    PreviewIn, PreviewOut, PreviewNodesOut, PreviewNodeOut, ReorderIn, ConnectBestIn,
    ImportNodesIn, ImportNodesOut, DetachIn, NodeValidateOut,
    SettingsOut, SettingsIn, DiagnosticsOut,
    ProfileIn, ProfileUpdate, ProfileOut, DefaultProfileIn,
    ProfileValidateOut, ProfilePresetInfo,
    RoutingIn, RoutingOut, RoutingRuleOut, RoutingValidateOut, PresetInfo, NodeHealthOut,
    NetworkOut, NetworkIn, NetworkSegmentOut, NetworkStatusOut, RouterRecOut,
    ConnEventOut, TrafficHistoryOut,
)
from pi_gw_panel.api.deps import get_state, require_auth, require_csrf
from pi_gw_panel.auth.auth import (
    SESSION_AUTHED, SESSION_CSRF, SESSION_EPOCH, SESSION_LASTSEEN, new_csrf_token)
from pi_gw_panel.auth import service as auth_service
from pi_gw_panel.models import Node, Subscription, TuningProfile, RoutingRule, NodeHealth
from pi_gw_panel.controller import (
    apply_node, apply_net, reapply_active_node, build_node_config, apply_lock, stop_net, sync_net)
from pi_gw_panel.net_control import netcheck, events as conn_events
from pi_gw_panel.health import probe
from pi_gw_panel.health.selection import best_node
from pi_gw_panel import backup as backup_mod
from pi_gw_panel import logs as logs_mod
from pi_gw_panel.xray_config.routing import PRESETS, preset_rules, validate_routing
from pi_gw_panel.xray_config.tuning import resolve_profile, validate_profile, PROFILE_PRESETS
from pi_gw_panel.xray_config.builder import build_config
from pi_gw_panel.xray_config.validate import ConfigManager, validate_config
from pi_gw_panel.config import SETTINGS_DEFAULTS
from pi_gw_panel.subs.inject import build_request, default_injection, host_tokens
from pi_gw_panel.subs import service
from pi_gw_panel.subs.fetcher import fetch
from pi_gw_panel.subs.parsers.dispatch import parse_subscription, detect

router = APIRouter(prefix="/api")


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
    return SettingsOut(
        tunneled_fetch=val("tunneled_fetch") == "1",
        routing_default_action=val("routing_default_action"),
        health_enabled=val("health_enabled") == "1",
        health_interval=int(val("health_interval")),
        health_hysteresis=int(val("health_hysteresis")),
        health_probe_url=val("health_probe_url"),
        failover_enabled=val("failover_enabled") == "1",
        failover_cooldown=int(val("failover_cooldown")),
        stats_enabled=val("stats_enabled") == "1",
        stats_api_port=int(val("stats_api_port")),
        traffic_sample_ms=int(val("traffic_sample_ms")),
        dns_intercept=val("dns_intercept") == "1",
        session_timeout_min=int(val("session_timeout_min")),
        auto_backup_enabled=val("auto_backup_enabled") == "1")


_NET_EDITABLE = ("segment_iface", "segment_ip", "dhcp_start", "dhcp_end", "dhcp_lease", "client_dns")


def _network_out(state) -> NetworkOut:
    store, settings = state.store, state.settings
    def ov(key: str) -> str:                       # editable field: DB override or config
        return store.get_setting(key) or getattr(settings, key)
    # C1: only the real Pi backend probes the uplink (a live socket); dev/CI = unknown.
    uplink_check = netcheck.uplink_up if type(state.net).__name__ == "LinuxBackend" else (lambda: None)
    kill_switch = (store.get_setting("kill_switch_enabled") or "0") == "1"
    running = state.supervisor.status().get("running", False)
    st = netcheck.network_status(store, settings, uplink_check=uplink_check)
    st["wan_blocked"] = kill_switch and not running   # N1: leak-guard holding while tunnel down
    return NetworkOut(
        segment=NetworkSegmentOut(
            iface=ov("segment_iface"), ip=ov("segment_ip"),
            dhcp_start=ov("dhcp_start"), dhcp_end=ov("dhcp_end"),
            dhcp_lease=ov("dhcp_lease"), client_dns=ov("client_dns")),
        kill_switch_enabled=kill_switch,
        status=NetworkStatusOut(**st),
        recommendations=[RouterRecOut(**r) for r in netcheck.router_recommendations(settings)],
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
    return {"needs_setup": auth_service.needs_setup(get_state(request).store)}


@router.post("/setup")
def setup_create(body: SetupIn, request: Request) -> dict:
    """Open by necessity (no credential exists yet) — creates the one-and-only
    credential and opens a session. 409 once configured (no re-setup via this route)."""
    state = get_state(request)
    if not auth_service.needs_setup(state.store):
        raise HTTPException(status_code=409, detail="already configured")
    auth_service.create_credential(state.store, body.username, body.password)
    _open_session(request)
    return {"ok": True}


@router.post("/login")
def login(body: LoginIn, request: Request) -> dict:
    state = get_state(request)
    guard = request.app.state.login_guard          # per-app counter (SS3)
    now = time.time()
    if guard["until"] > now:
        raise HTTPException(status_code=429, detail="too many attempts — try again shortly")
    if not auth_service.verify_login(state.store, body.username, body.password):
        guard["count"] += 1
        if guard["count"] >= 5:
            guard["until"] = now + 60               # lock out for 60s after 5 fails
            guard["count"] = 0
        raise HTTPException(status_code=401, detail="bad credentials")
    guard["count"] = 0
    _open_session(request)
    return {"ok": True}


@router.post("/logout")
def logout(request: Request) -> dict:
    request.session.clear()
    return {"ok": True}


@router.post("/password")
def change_password(body: PasswordChangeIn, request: Request,
                    _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    state = get_state(request)
    username = state.store.get_setting("auth_username") or ""
    if not auth_service.verify_login(state.store, username, body.current_password):
        raise HTTPException(status_code=403, detail="current password incorrect")
    auth_service.set_password(state.store, body.new_password)
    # invalidate other sessions, then keep the current one valid (SS3)
    request.session[SESSION_EPOCH] = auth_service.bump_session_epoch(state.store)
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
    return StatusOut(running=st["running"], pid=st["pid"],
                     active_node_id=int(active) if active else None,  # "" (post-rollback) → None
                     xray_state=state.supervisor.state(),
                     active_since=int(since) if since else None,
                     last_failover_at=float(last_fo) if last_fo else None,
                     server_now=time.time())   # D4: client offsets freshness/uptime by this


@router.get("/traffic/history", response_model=TrafficHistoryOut)
def traffic_history(request: Request, window_sec: int = 3600, max_points: int = 600,
                    _: None = Depends(require_auth)) -> TrafficHistoryOut:
    """Seed the Dashboard graph with the recorded proxy throughput over the last
    `window_sec`, downsampled to at most `max_points` points (proxy outbound)."""
    state = get_state(request)
    interval = int(state.store.get_setting("traffic_sample_ms") or SETTINGS_DEFAULTS["traffic_sample_ms"])
    hist = getattr(state, "history", None)
    if hist is None:
        return TrafficHistoryOut(samples=[], interval_ms=interval)
    since = int(datetime.now(timezone.utc).timestamp() * 1000) - max(1, window_sec) * 1000
    series = hist.series(since_ms=since, max_points=max(1, max_points))
    return TrafficHistoryOut(samples=[[s[0], s[1], s[2]] for s in series], interval_ms=interval)


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
    nid = state.store.add_node(node)
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
    # exclude_unset (not exclude_none) so an explicit `tuning_profile_id: null` clears
    # the assignment (→ inherit the global default) rather than being dropped.
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(node, k, v)
    # single source of truth: re-derive network/security/flow from the edited fields
    node.normalize()
    state.store.update_node(node)
    return _node_out(state.store.get_node(node_id))


@router.delete("/nodes/{node_id}")
def delete_node(node_id: int, request: Request,
                _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    state = get_state(request)
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
    """Disconnect the active node and clear the active selection. The net rules come down
    via stop_net: with the kill-switch ON the fail-closed leak-guard stays in place (so
    'disconnect' doesn't leak client→WAN — A1); with it off, clients fall back to direct.
    xray is left running — the sidebar toggle is the only thing that stops xray-core."""
    state = get_state(request)
    with apply_lock:   # don't race a concurrent apply / failover tick (NR1)
        stop_net(state.settings, state.net, state.store)
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
    otherwise just start the process."""
    state = get_state(request)
    with apply_lock:   # reapply_active_node re-enters the lock (RLock); guards plain start()
        res = reapply_active_node(state)
        if res is None:
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
        state.supervisor.stop()
        stop_net(state.settings, state.net, state.store)
    conn_events.record(state.store, "xray-stop", "xray-core stopped")
    return {"ok": True}


@router.post("/rollback")
def rollback(request: Request,
             _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    state = get_state(request)
    with apply_lock:
        ok = ConfigManager(state.settings, xray_bin=state.xray_bin).rollback()
        if ok:
            state.supervisor.reload()
            prev = state.store.get_setting("prev_active_node_id")
            state.store.set_setting("active_node_id", prev if prev else "")  # revert to prior apply
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
    before = _active_resolved_pid(state.store)
    state.store.set_default_profile(body.id)
    if _active_resolved_pid(state.store) != before:   # active node inherits the default → re-apply
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
    state.store.update_profile(p)
    if _active_resolved_pid(state.store) == profile_id:   # edited the live profile → re-apply (TB1)
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
    state.store.replace_routing(rules)
    state.store.set_setting("routing_default_action", body.default_action)
    state.store.set_setting("routing_domain_strategy", body.domain_strategy)
    # Apply now: rebuild the live config (which embeds routing) + reload xray, so the change
    # takes effect immediately instead of silently waiting for the next Connect. No-op when
    # no node is active; a rule xray rejects surfaces as 502 (live config stays last-good).
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
    return [NodeHealthOut(**vars(h)) for h in state.store.list_health()]


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
        return nodes
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
    return [NodeHealthOut(**vars(h)) for h in store.list_health()]


@router.post("/probe/tcp", response_model=list[NodeHealthOut])
def probe_tcp(request: Request, scope: str | None = None,
              _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> list[NodeHealthOut]:
    """TCP-ping nodes (reachability + latency) on demand → updates the TCP column. `scope`
    limits the sweep to a subscription / manual group (default all)."""
    store = get_state(request).store
    def assign(h: NodeHealth, ok, ms): h.last_tcp_ok, h.last_tcp_ms = ok, ms
    return _probe_sweep(store, _scoped_nodes(store, scope),
                        lambda n: probe.tcp_ping(n.address, n.port, timeout=2.0), assign)


@router.post("/probe/http", response_model=list[NodeHealthOut])
def probe_http(request: Request, scope: str | None = None,
               _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> list[NodeHealthOut]:
    """HTTPS-handshake each node directly (not through the tunnel) → HTTP column. `scope`
    limits the sweep to a subscription / manual group (default all)."""
    store = get_state(request).store
    def assign(h: NodeHealth, ok, ms): h.last_http_ok, h.last_http_ms = ok, ms
    return _probe_sweep(store, _scoped_nodes(store, scope),
                        lambda n: probe.http_ping(n.address, n.port, n.sni, timeout=3.0),
                        assign, record_http=True)


@router.post("/nodes/{node_id}/probe", response_model=NodeHealthOut)
def probe_node(node_id: int, request: Request, real_only: bool = False,
               _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> NodeHealthOut:
    """Per-node 'T': run all three probes for one node — TCP, direct HTTPS, and a real request
    *through* this node (throwaway xray, so the live tunnel is untouched) — and persist them.

    `real_only` skips the two DIRECT probes (TCP + TLS handshake) and refreshes only the
    real-request latency/egress — used by the Dashboard's 60s active-node liveness loop, which
    consumes nothing else, so the direct dials are wasted load otherwise (audit D6)."""
    state = get_state(request)
    store = state.store
    node = store.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="node not found")
    probe_url = store.get_setting("health_probe_url") or SETTINGS_DEFAULTS["health_probe_url"]
    active = store.get_setting("active_node_id")
    tunneled_on = (store.get_setting("tunneled_fetch") or "1") == "1"
    if (active and int(active) == node_id and tunneled_on
            and state.supervisor.status().get("running")):
        # NR3: this IS the live node — reuse the running tunnel's proxy instead of spawning a
        # second throwaway xray that dials the same server.
        proxy_url = f"http://127.0.0.1:{state.settings.local_proxy_port}"
        real_ok, _status, real_ms, egress = probe.real_request(proxy_url, probe_url)
    else:
        real_ok, real_ms, egress = probe.real_through_node(node, state.xray_bin, probe_url)
    h = store.get_health(node_id) or NodeHealth(node_id=node_id)
    if not real_only:
        h.last_tcp_ok, h.last_tcp_ms = probe.tcp_ping(node.address, node.port, timeout=3.0)
        h.last_http_ok, h.last_http_ms = probe.http_ping(node.address, node.port, node.sni, timeout=4.0)
    h.last_real_ok, h.last_real_ms, h.egress_ip = real_ok, real_ms, egress
    h.checked_at = datetime.now(timezone.utc).isoformat()
    store.upsert_health(h)
    if real_only:
        if real_ok and real_ms is not None:
            store.record_latency(node_id, real_ms)   # feed the dashboard latency sparkline (B)
    elif h.last_http_ok and h.last_http_ms is not None:
        store.record_latency(node_id, h.last_http_ms)   # NN4
    return NodeHealthOut(**vars(store.get_health(node_id)))


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
    return backup_mod.export_state(get_state(request).store)


@router.post("/restore")
def post_restore(body: dict, request: Request,
                 _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    try:
        summary = backup_mod.import_state(get_state(request).store, body)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid backup: {exc}")
    return {"ok": True, "restored": summary}


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
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(sub, k, v)
    sub.interval_sec = _clamp_interval(sub.interval_sec)
    state.store.update_subscription(sub)
    return _sub_out(state, state.store.get_subscription(sub_id))


@router.delete("/subs/{sub_id}")
def delete_sub(sub_id: int, request: Request,
               _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    state = get_state(request)
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
    injection = body.injection if body.injection is not None else default_injection()
    req = build_request(body.url, injection, host_tokens(service.machine_id()))
    return PreviewOut(method=req.method, url=req.url, headers=req.headers, query=req.query)


@router.post("/subs/preview-nodes", response_model=PreviewNodesOut)
def preview_sub_nodes(body: PreviewIn, request: Request,
                      _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> PreviewNodesOut:
    """N1: dry-run — fetch + parse without persisting, so a bad URL/token/format is caught
    before adding/saving. Uses the tunnel when one is up, like a real refresh."""
    state = get_state(request)
    injection = body.injection if body.injection is not None else default_injection()
    tokens = host_tokens(service.machine_id())
    try:
        text, _path, _headers = fetch(body.url, injection, tokens, proxy=service.tunnel_proxy(state))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"fetch failed: {exc}")
    try:
        fmt = detect(text)
        nodes = parse_subscription(text)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"parse failed: {exc}")
    return PreviewNodesOut(
        format=fmt, count=len(nodes),
        nodes=[PreviewNodeOut(name=n.name, address=n.address, port=n.port,
                              transport=n.transport, network=n.network, security=n.security)
               for n in nodes[:200]])


@router.post("/subs/refresh-all")
def refresh_all_subs(request: Request,
                     _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    """N3: refresh every enabled subscription now. Disabled subs are skipped."""
    state = get_state(request)
    results = {}
    for sub in state.store.list_subscriptions():
        if sub.enabled:
            results[sub.id] = service.refresh(state, sub)
    return {"refreshed": len(results), "results": results}


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
def validate_node(body: NodeIn, request: Request,
                  _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> NodeValidateOut:
    """NN10: pre-flight — build this node's config and run `xray -test` without connecting,
    so a bad reality/xhttp/tls combo is caught before Connect."""
    state = get_state(request)
    node = Node(id=None, name=body.name, address=body.address, port=body.port,
                uuid=body.uuid, transport=body.transport, security=body.security,
                sni=body.sni, public_key=body.public_key, short_id=body.short_id,
                fingerprint=body.fingerprint, path=body.path, host=body.host,
                mode=body.mode, alpn=body.alpn)
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
        parsed = parse_subscription(body.text)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"parse failed: {exc}")
    added = 0
    for p in parsed[:service.MAX_NODES]:
        if state.store.get_node_by_identity(None, p.address, p.port, p.uuid, p.path) is not None:
            continue
        p.id = None
        p.subscription_id = None
        p.stale = False
        state.store.add_node(p)
        added += 1
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
_SETTINGS_RESET_KEYS = ("tunneled_fetch", "dns_intercept", "health_enabled", "health_interval",
                        "health_hysteresis", "health_probe_url", "failover_enabled",
                        "failover_cooldown", "stats_enabled", "stats_api_port",
                        "traffic_sample_ms", "session_timeout_min", "auto_backup_enabled")


def _validate_settings(state, data: dict) -> None:
    """SC2: reject out-of-range values that would break the runtime (busy loops, bad ports)."""
    floors = {"health_interval": 60, "traffic_sample_ms": 250, "health_hysteresis": 1,
              "failover_cooldown": 0, "session_timeout_min": 0}
    for k, lo in floors.items():
        if isinstance(data.get(k), int) and data[k] < lo:
            raise HTTPException(status_code=422, detail=f"{k} must be >= {lo}")
    if "stats_api_port" in data:
        p = data["stats_api_port"]
        if not (1 <= p <= 65535):
            raise HTTPException(status_code=422, detail="stats_api_port must be 1..65535")
        if p in (state.settings.tproxy_port, state.settings.local_proxy_port):
            raise HTTPException(status_code=422, detail="stats_api_port collides with a system port")


@router.put("/settings", response_model=SettingsOut)
def put_settings(body: SettingsIn, request: Request,
                 _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> SettingsOut:
    state = get_state(request)
    data = body.model_dump(exclude_none=True)
    _validate_settings(state, data)
    for k, v in data.items():
        state.store.set_setting(k, ("1" if v else "0") if isinstance(v, bool) else str(v))
    if _SETTINGS_CONFIG_KEYS & data.keys():   # SR1: a config-affecting toggle → re-apply live
        _reapply_or_502(state)
    return _settings_out(state)


@router.post("/settings/reset", response_model=SettingsOut)
def reset_settings(request: Request,
                   _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> SettingsOut:
    """SN1: restore the Settings-screen knobs to their defaults (routing-owned keys untouched)."""
    state = get_state(request)
    for k in _SETTINGS_RESET_KEYS:
        state.store.set_setting(k, SETTINGS_DEFAULTS[k])
    _reapply_or_502(state)
    return _settings_out(state)


@router.get("/diagnostics", response_model=DiagnosticsOut)
def diagnostics(request: Request, _: None = Depends(require_auth)) -> DiagnosticsOut:
    """SN8: at-a-glance support info — app/xray version, uptime, DB size, disk."""
    state = get_state(request)
    from importlib.metadata import version, PackageNotFoundError
    try:
        app_v = version("pi-gw-panel") or "unknown"
    except (PackageNotFoundError, Exception):
        app_v = "unknown"
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
    return DiagnosticsOut(app_version=app_v, xray_version=xray_v,
                          uptime_sec=int(time.time() - _START_TIME), db_path=db, db_bytes=db_bytes,
                          disk_free_bytes=du.free, disk_total_bytes=du.total)


# --- network (editable Pi net config + kill-switch + live status + router guidance) ---
@router.get("/network", response_model=NetworkOut)
def get_network(request: Request, _: None = Depends(require_auth)) -> NetworkOut:
    return _network_out(get_state(request))


@router.put("/network", response_model=NetworkOut)
def put_network(body: NetworkIn, request: Request,
                _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> NetworkOut:
    state = get_state(request)
    data = body.model_dump(exclude_none=True)
    for k in _NET_EDITABLE:
        if k in data:
            state.store.set_setting(k, data[k])
    if "kill_switch_enabled" in data:
        was = (state.store.get_setting("kill_switch_enabled") or "0") == "1"
        now_on = bool(data["kill_switch_enabled"])
        state.store.set_setting("kill_switch_enabled", "1" if now_on else "0")
        if now_on != was:
            conn_events.record(state.store, "kill-switch", "enabled" if now_on else "disabled")
    # Apply the edit to match the CURRENT tunnel state (tproxy if up, else leak-guard/teardown)
    # so toggling the kill-switch while disconnected installs the guard, not a dead tproxy (A1).
    sync_net(state)
    return _network_out(state)
