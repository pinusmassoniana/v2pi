from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Request, HTTPException
from pi_gw_panel.api.schemas import (
    LoginIn, SetupIn, PasswordChangeIn, NodeIn, NodeOut, NodeUpdate, StatusOut,
    SubscriptionIn, SubscriptionPatch, SubscriptionOut,
    PreviewIn, PreviewOut, SettingsOut, SettingsIn,
    ProfileIn, ProfileUpdate, ProfileOut, DefaultProfileIn,
    RoutingIn, RoutingOut, RoutingRuleOut, NodeHealthOut,
    NetworkOut, NetworkIn, NetworkSegmentOut, NetworkStatusOut, RouterRecOut,
    TrafficHistoryOut,
)
from pi_gw_panel.api.deps import get_state, require_auth, require_csrf
from pi_gw_panel.auth.auth import SESSION_AUTHED, SESSION_CSRF, new_csrf_token
from pi_gw_panel.auth import service as auth_service
from pi_gw_panel.models import Node, Subscription, TuningProfile, RoutingRule, NodeHealth
from pi_gw_panel.controller import apply_node, apply_net
from pi_gw_panel.net_control import netcheck
from pi_gw_panel.health import probe
from pi_gw_panel import backup as backup_mod
from pi_gw_panel import logs as logs_mod
from pi_gw_panel.xray_config.routing import RU_DIRECT_PRESET
from pi_gw_panel.xray_config.validate import ConfigManager
from pi_gw_panel.config import SETTINGS_DEFAULTS
from pi_gw_panel.subs.inject import build_request, default_injection, host_tokens
from pi_gw_panel.subs import service

router = APIRouter(prefix="/api")


def _node_out(n: Node) -> NodeOut:
    return NodeOut(id=n.id, name=n.name, address=n.address, port=n.port, uuid=n.uuid,
                   transport=n.transport, sni=n.sni, public_key=n.public_key,
                   short_id=n.short_id, fingerprint=n.fingerprint,
                   subscription_id=n.subscription_id, stale=n.stale,
                   tuning_profile_id=n.tuning_profile_id)


_PROFILE_FIELDS = ("id", "name", "fingerprint", "frag_enabled", "frag_packets",
                   "frag_length", "frag_interval", "mux_enabled", "doh_enabled",
                   "doh_url", "quic")


def _profile_out(p: TuningProfile, default_id: int | None) -> ProfileOut:
    return ProfileOut(is_default=(default_id is not None and default_id == p.id),
                      **{f: getattr(p, f) for f in _PROFILE_FIELDS})


def _routing_out(state) -> RoutingOut:
    return RoutingOut(
        rules=[RoutingRuleOut(id=r.id, position=r.position, type=r.type,
                              value=r.value, action=r.action)
               for r in state.store.get_routing()],
        default_action=state.store.get_setting("routing_default_action") or "proxy")


def _sub_out(state, sub: Subscription) -> SubscriptionOut:
    return SubscriptionOut(
        id=sub.id, name=sub.name, url=sub.url, injection=sub.injection,
        interval_sec=sub.interval_sec, last_fetched=sub.last_fetched,
        last_status=sub.last_status, last_path=sub.last_path,
        node_count=len(state.store.list_nodes_for_sub(sub.id)))


def _settings_out(state) -> SettingsOut:
    def val(key: str) -> str:
        return state.store.get_setting(key) or SETTINGS_DEFAULTS[key]
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
        traffic_sample_ms=int(val("traffic_sample_ms")))


_NET_EDITABLE = ("segment_iface", "segment_ip", "dhcp_start", "dhcp_end", "dhcp_lease", "client_dns")


def _network_out(state) -> NetworkOut:
    store, settings = state.store, state.settings
    def ov(key: str) -> str:                       # editable field: DB override or config
        return store.get_setting(key) or getattr(settings, key)
    return NetworkOut(
        segment=NetworkSegmentOut(
            iface=ov("segment_iface"), ip=ov("segment_ip"),
            dhcp_start=ov("dhcp_start"), dhcp_end=ov("dhcp_end"),
            dhcp_lease=ov("dhcp_lease"), client_dns=ov("client_dns")),
        kill_switch_enabled=(store.get_setting("kill_switch_enabled") or "0") == "1",
        status=NetworkStatusOut(**netcheck.network_status(store, settings)),
        recommendations=[RouterRecOut(**r) for r in netcheck.router_recommendations(settings)])


def _open_session(request: Request) -> None:
    request.session[SESSION_AUTHED] = True
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
    if not auth_service.verify_login(state.store, body.username, body.password):
        raise HTTPException(status_code=401, detail="bad credentials")
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
    return StatusOut(running=st["running"], pid=st["pid"],
                     active_node_id=int(active) if active else None,  # "" (post-rollback) → None
                     xray_state=state.supervisor.state(),
                     active_since=int(since) if since else None)


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
    node = Node(id=None, name=body.name, address=body.address, port=body.port,
                uuid=body.uuid, transport=body.transport, sni=body.sni,
                public_key=body.public_key, short_id=body.short_id,
                fingerprint=body.fingerprint)
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
    # keep the Vision-only flow invariant after a transport change
    node.flow = "" if node.transport != "vision" else (node.flow or "xtls-rprx-vision")
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
    return {"ok": True}


@router.post("/nodes/{node_id}/disconnect")
def disconnect(node_id: int, request: Request,
               _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    """Disconnect the active node: tear down the tproxy/routing (segment clients fall back
    to direct) and clear the active selection. xray is left running — the sidebar toggle is
    the only thing that stops xray-core."""
    state = get_state(request)
    state.net.teardown()
    prev = state.store.get_setting("active_node_id")
    state.store.set_setting("prev_active_node_id", prev or "")
    state.store.set_setting("active_node_id", "")
    state.store.set_setting("active_since", "")        # clear uptime anchor (P3)
    return {"ok": True}


@router.post("/xray/start")
def xray_start(request: Request,
               _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    """Start xray-core. If a node is active, bring its tunnel back up (config + net);
    otherwise just start the process."""
    state = get_state(request)
    from pi_gw_panel.controller import reapply_active_node
    res = reapply_active_node(state)
    if res is None:
        state.supervisor.start()
    elif not res.ok:
        raise HTTPException(status_code=502, detail=res.error)
    return {"ok": True}


@router.post("/xray/stop")
def xray_stop(request: Request,
              _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    """Stop xray-core and tear down the tproxy/routing (so segment clients fall back to
    direct rather than black-holing into a dead tproxy port). The active selection is kept."""
    state = get_state(request)
    state.supervisor.stop()
    state.net.teardown()
    return {"ok": True}


@router.post("/rollback")
def rollback(request: Request,
             _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    state = get_state(request)
    ok = ConfigManager(state.settings, xray_bin=state.xray_bin).rollback()
    if ok:
        state.supervisor.reload()
        prev = state.store.get_setting("prev_active_node_id")
        state.store.set_setting("active_node_id", prev if prev else "")  # revert to prior apply
        state.store.set_setting(
            "active_since", str(int(datetime.now(timezone.utc).timestamp())) if prev else "")
    return {"ok": ok}


# --- tuning profiles ---
@router.get("/profiles", response_model=list[ProfileOut])
def list_profiles(request: Request, _: None = Depends(require_auth)) -> list[ProfileOut]:
    state = get_state(request)
    d = state.store.get_default_profile()
    did = d.id if d else None
    return [_profile_out(p, did) for p in state.store.list_profiles()]


@router.post("/profiles", response_model=ProfileOut)
def add_profile(body: ProfileIn, request: Request,
                _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> ProfileOut:
    state = get_state(request)
    pid = state.store.add_profile(TuningProfile(id=None, **body.model_dump()))
    d = state.store.get_default_profile()
    return _profile_out(state.store.get_profile(pid), d.id if d else None)


@router.put("/profiles/default", response_model=ProfileOut)
def set_default_profile(body: DefaultProfileIn, request: Request,
                        _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> ProfileOut:
    state = get_state(request)
    p = state.store.get_profile(body.id)
    if p is None:
        raise HTTPException(status_code=404, detail="profile not found")
    state.store.set_default_profile(body.id)
    return _profile_out(p, body.id)


@router.patch("/profiles/{profile_id}", response_model=ProfileOut)
def update_profile(profile_id: int, body: ProfileUpdate, request: Request,
                   _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> ProfileOut:
    state = get_state(request)
    p = state.store.get_profile(profile_id)
    if p is None:
        raise HTTPException(status_code=404, detail="profile not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(p, k, v)
    state.store.update_profile(p)
    d = state.store.get_default_profile()
    return _profile_out(state.store.get_profile(profile_id), d.id if d else None)


@router.delete("/profiles/{profile_id}")
def delete_profile(profile_id: int, request: Request,
                   _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> dict:
    state = get_state(request)
    d = state.store.get_default_profile()
    if d is not None and d.id == profile_id:
        # the global default must always exist (nodes inherit it); reassign default first
        raise HTTPException(status_code=409, detail="cannot delete the default profile")
    state.store.delete_profile(profile_id)
    return {"ok": True}


# --- routing ---
@router.get("/routing", response_model=RoutingOut)
def get_routing(request: Request, _: None = Depends(require_auth)) -> RoutingOut:
    return _routing_out(get_state(request))


@router.put("/routing", response_model=RoutingOut)
def put_routing(body: RoutingIn, request: Request,
                _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> RoutingOut:
    state = get_state(request)
    state.store.replace_routing(
        [RoutingRule(id=None, position=i, type=r.type, value=r.value, action=r.action)
         for i, r in enumerate(body.rules)])
    state.store.set_setting("routing_default_action", body.default_action)
    return _routing_out(state)


@router.post("/routing/preset/ru-direct", response_model=RoutingOut)
def routing_preset_ru_direct(request: Request,
                             _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> RoutingOut:
    # Additive + idempotent: append the preset's rules that aren't already present.
    state = get_state(request)
    existing = state.store.get_routing()
    have = {(r.type, r.value, r.action) for r in existing}
    combined = list(existing) + [r for r in RU_DIRECT_PRESET
                                 if (r.type, r.value, r.action) not in have]
    state.store.replace_routing(combined)
    return _routing_out(state)


# --- node health (per-node snapshot; distinct from the open /api/health liveness) ---
@router.get("/node-health", response_model=list[NodeHealthOut])
def node_health(request: Request, _: None = Depends(require_auth)) -> list[NodeHealthOut]:
    state = get_state(request)
    return [NodeHealthOut(**vars(h)) for h in state.store.list_health()]


def _probe_sweep(store, probe_one, assign) -> list[NodeHealthOut]:
    """Probe every node concurrently, persist the result (preserving the fields the other
    sweep owns), and return the full updated health list."""
    nodes = store.list_nodes()
    ts = datetime.now(timezone.utc).isoformat()
    with ThreadPoolExecutor(max_workers=24) as ex:
        results = list(ex.map(lambda n: (n.id, probe_one(n)), nodes))
    for nid, (ok, ms) in results:
        h = store.get_health(nid) or NodeHealth(node_id=nid)
        assign(h, ok, ms)
        h.checked_at = ts
        store.upsert_health(h)
    return [NodeHealthOut(**vars(h)) for h in store.list_health()]


@router.post("/probe/tcp", response_model=list[NodeHealthOut])
def probe_tcp(request: Request,
              _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> list[NodeHealthOut]:
    """TCP-ping every node (reachability + latency) on demand → updates the TCP column."""
    store = get_state(request).store
    def assign(h: NodeHealth, ok, ms): h.last_tcp_ok, h.last_tcp_ms = ok, ms
    return _probe_sweep(store, lambda n: probe.tcp_ping(n.address, n.port, timeout=2.0), assign)


@router.post("/probe/http", response_model=list[NodeHealthOut])
def probe_http(request: Request,
               _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> list[NodeHealthOut]:
    """HTTPS-handshake every node endpoint directly (not through the tunnel) → HTTP column."""
    store = get_state(request).store
    def assign(h: NodeHealth, ok, ms): h.last_http_ok, h.last_http_ms = ok, ms
    return _probe_sweep(store, lambda n: probe.http_ping(n.address, n.port, n.sni, timeout=3.0), assign)


@router.post("/nodes/{node_id}/probe", response_model=NodeHealthOut)
def probe_node(node_id: int, request: Request,
               _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> NodeHealthOut:
    """Per-node 'T': run all three probes for one node — TCP, direct HTTPS, and a real request
    *through* this node (throwaway xray, so the live tunnel is untouched) — and persist them."""
    state = get_state(request)
    store = state.store
    node = store.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="node not found")
    tcp_ok, tcp_ms = probe.tcp_ping(node.address, node.port, timeout=3.0)
    http_ok, http_ms = probe.http_ping(node.address, node.port, node.sni, timeout=4.0)
    probe_url = store.get_setting("health_probe_url") or SETTINGS_DEFAULTS["health_probe_url"]
    real_ok, real_ms, egress = probe.real_through_node(node, state.xray_bin, probe_url)
    h = store.get_health(node_id) or NodeHealth(node_id=node_id)
    h.last_tcp_ok, h.last_tcp_ms = tcp_ok, tcp_ms
    h.last_http_ok, h.last_http_ms = http_ok, http_ms
    h.last_real_ok, h.last_real_ms, h.egress_ip = real_ok, real_ms, egress
    h.checked_at = datetime.now(timezone.utc).isoformat()
    store.upsert_health(h)
    return NodeHealthOut(**vars(h))


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
    return [_sub_out(state, s) for s in state.store.list_subscriptions()]


@router.post("/subs", response_model=SubscriptionOut)
def add_sub(body: SubscriptionIn, request: Request,
            _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> SubscriptionOut:
    state = get_state(request)
    injection = body.injection if body.injection is not None else default_injection()
    sid = state.store.add_subscription(Subscription(
        id=None, name=body.name, url=body.url, injection=injection,
        interval_sec=body.interval_sec))
    return _sub_out(state, state.store.get_subscription(sid))


@router.patch("/subs/{sub_id}", response_model=SubscriptionOut)
def update_sub(sub_id: int, body: SubscriptionPatch, request: Request,
               _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> SubscriptionOut:
    state = get_state(request)
    sub = state.store.get_subscription(sub_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="subscription not found")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(sub, k, v)
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


# --- settings (global toggles) ---
@router.get("/settings", response_model=SettingsOut)
def get_settings(request: Request, _: None = Depends(require_auth)) -> SettingsOut:
    return _settings_out(get_state(request))


@router.put("/settings", response_model=SettingsOut)
def put_settings(body: SettingsIn, request: Request,
                 _: None = Depends(require_auth), __: None = Depends(require_csrf)) -> SettingsOut:
    state = get_state(request)
    for k, v in body.model_dump(exclude_none=True).items():
        state.store.set_setting(k, ("1" if v else "0") if isinstance(v, bool) else str(v))
    return _settings_out(state)


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
        state.store.set_setting("kill_switch_enabled", "1" if data["kill_switch_enabled"] else "0")
    apply_net(state.settings, state.net, state.store)   # apply the edit immediately (re-render)
    return _network_out(state)
