export interface Status { running: boolean; pid: number | null; active_node_id: number | null; xray_state: string; active_since: number | null; last_failover_at: number | null; prev_active_node_id: number | null; server_now: number; }
export interface Node {
  id: number; name: string; address: string; port: number; uuid: string; transport: string;
  network: string; security: string;
  sni: string; public_key: string; short_id: string; fingerprint: string;
  path: string; host: string; mode: string; alpn: string; note: string;
  subscription_id: number | null; stale: boolean; tuning_profile_id: number | null;
}
export interface NodeIn {
  name: string; address: string; port: number; uuid: string;
  transport?: string; security?: string; sni?: string; public_key?: string; short_id?: string;
  fingerprint?: string; path?: string; host?: string; mode?: string; alpn?: string; note?: string;
}
// tuning_profile_id is patch-only (assign a profile, or null to inherit the global default)
export type NodeUpdate = Partial<NodeIn> & { tuning_profile_id?: number | null };

export interface Subscription {
  id: number; name: string; url: string; injection: Record<string, any>;
  interval_sec: number; enabled: boolean; default_profile_id: number | null;
  last_fetched: string | null; last_status: string | null; last_path: string | null;
  last_error: string | null;
  up_bytes: number | null; down_bytes: number | null; total_bytes: number | null; expire_at: number | null;
  node_count: number;
}
export interface SubscriptionIn {
  name: string; url: string; interval_sec?: number; injection?: Record<string, any>;
  enabled?: boolean; default_profile_id?: number | null;
}
export interface Preview { method: string; url: string; headers: Record<string, string>; query: Record<string, string>; }
export interface PreviewNode { name: string; address: string; port: number; transport: string; network: string; security: string; }
export interface PreviewNodes { format: string; count: number; nodes: PreviewNode[]; }
export interface Settings {
  tunneled_fetch: boolean; routing_default_action: string;
  health_enabled: boolean; health_interval: number; health_hysteresis: number; health_probe_url: string;
  failover_enabled: boolean; failover_cooldown: number;
  stats_enabled: boolean; stats_api_port: number; traffic_sample_ms: number;
  dns_intercept: boolean; session_timeout_min: number; auto_backup_enabled: boolean;
}
export interface Diagnostics {
  app_version: string; xray_version: string; uptime_sec: number;
  db_path: string; db_bytes: number; disk_free_bytes: number; disk_total_bytes: number;
}

// --- Wave 3a: live traffic graph ---
export interface OutboundRate { up_bps: number; down_bps: number; }
export interface TrafficFrame {
  ts: number;
  outbounds: Record<string, OutboundRate>;
  totals: { up: number; down: number };   // proxy outbound bytes since xray start (resets on restart)
  lifetime?: { up: number; down: number };  // durable data-used total, survives xray restart (F)
  session?: { up: number; down: number };   // data used since the last (re)connect (NF4)
  active: { node_id: number; real_ok: boolean | null; latency_ms: number | null; egress_ip: string | null; egress_ip6: string | null; egress_cc: string | null; egress_cc6: string | null; checked_at: string | null; lat_history: number[] } | null;
}
export type TrafficMessage = TrafficFrame | { disabled: true } | { error: string };
// long-window history seed: each sample is [ts_ms, up_bps, down_bps]
export interface TrafficHistoryResp { samples: number[][]; interval_ms: number; }
export type BackupDoc = Record<string, any>;

// --- Wave 2: tuning profiles ---
export interface NoiseSpec { type: string; packet: string; delay: string; }
export interface TuningProfile {
  id: number; name: string; fingerprint: string;
  frag_enabled: boolean; frag_packets: string; frag_length: string; frag_interval: string;
  mux_enabled: boolean; doh_enabled: boolean; doh_url: string; quic: string;
  noise_enabled: boolean; noises: NoiseSpec[];
  xhttp_padding: string; xmux_max_concurrency: string; xmux_max_connections: string;
  mux_concurrency: string; xudp_proxy_udp443: string; alpn: string; tls_min: string; tls_max: string;
  is_default: boolean; is_active: boolean; node_count: number;
}
export interface ProfileIn {
  name: string; fingerprint?: string;
  frag_enabled?: boolean; frag_packets?: string; frag_length?: string; frag_interval?: string;
  mux_enabled?: boolean; doh_enabled?: boolean; doh_url?: string; quic?: string;
  noise_enabled?: boolean; noises?: NoiseSpec[];
  xhttp_padding?: string; xmux_max_concurrency?: string; xmux_max_connections?: string;
  mux_concurrency?: string; xudp_proxy_udp443?: string; alpn?: string; tls_min?: string; tls_max?: string;
}
export type ProfileUpdate = Partial<ProfileIn>;
export interface ProfilePreset { name: string; title: string; fields: Record<string, any>; }

// --- Wave 2: routing ---
export interface RoutingRule { id: number; position: number; type: string; value: string; action: string; enabled: boolean; label: string; }
export interface RoutingRuleIn { type: string; value: string; action: string; enabled?: boolean; label?: string; }
export interface Routing { rules: RoutingRule[]; default_action: string; domain_strategy: string; }
export interface RoutingIn { rules: RoutingRuleIn[]; default_action: string; domain_strategy?: string; }
export interface PresetInfo { name: string; title: string; }

// --- Wave 2: per-node health ---
export interface NodeHealth {
  node_id: number; last_tcp_ok: boolean | null; last_tcp_ms: number | null;
  last_http_ok: boolean | null; last_http_ms: number | null;
  last_real_ok: boolean | null; last_real_ms: number | null;
  egress_ip: string | null; egress_ip6: string | null;
  egress_cc: string | null; egress_cc6: string | null; checked_at: string | null; fail_count: number;
  lat_history: number[];
}

// --- Wave 3b: editable Pi network config + kill-switch + live status ---
export interface NetworkSegment {
  iface: string; ip: string; ip6: string; dhcp_start: string; dhcp_end: string; dhcp_lease: string;
  client_dns: string; client_dns6: string;
}
export interface NetworkTunnel { real_ok: boolean | null; latency_ms: number | null; egress_ip: string | null; checked_at: string | null; }
export interface DhcpClient { ip: string; mac: string; hostname: string; expiry: number; }
export interface NetworkStatus {
  segment_up: boolean | null; uplink: boolean | null; uplink6: boolean | null; dhcp_clients: number;
  clients: DhcpClient[]; tunnel: NetworkTunnel; wan_blocked: boolean;
  ipv6_prefix: string | null;   // DHCPv6-PD 'auto': host-delegated segment v6 prefix
  foreign_ra: boolean | null;   // another router advertising v6 on the segment (leak)
  ipv6_prefix_source: string | null;   // "static" | "ula" | "pd"
}
export interface RouterRec { title: string; detail: string; }
export interface ConnEvent { ts: number; kind: string; detail: string; }
export interface Network {
  segment: NetworkSegment; kill_switch_enabled: boolean; lan_access_enabled: boolean; ipv6_enabled: boolean;
  status: NetworkStatus; recommendations: RouterRec[]; events: ConnEvent[];
}
// PUT is partial + flat: editable settings keys (segment_iface/ip is long-form here) + toggles.
export interface NetworkPatch {
  segment_iface?: string; segment_ip?: string; segment_ip6?: string;
  dhcp_start?: string; dhcp_end?: string; dhcp_lease?: string; client_dns?: string; client_dns6?: string;
  kill_switch_enabled?: boolean; lan_access_enabled?: boolean; ipv6_enabled?: boolean;
}

export class ApiError extends Error { constructor(public status: number, msg: string) { super(msg); } }

let _csrf: string | null = null;

async function req(path: string, opts: RequestInit = {}): Promise<any> {
  const res = await fetch(`/api${path}`, { credentials: "include", ...opts });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, body?.detail ?? `HTTP ${res.status}`);
  return body;
}

async function ensureCsrf(): Promise<string> {
  if (_csrf) return _csrf;
  const { csrf } = await req("/csrf");
  _csrf = csrf;
  return csrf;
}

async function mutate(method: string, path: string, body?: unknown): Promise<any> {
  const headers: Record<string, string> = { "X-CSRF-Token": await ensureCsrf() };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  return req(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
}

function _postJson(path: string, body: unknown) {
  return req(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

export const api = {
  _reset() { _csrf = null; },
  ensureCsrf,
  // first-run setup: creates the credential AND opens a session (no prior auth needed)
  getSetup(): Promise<{ needs_setup: boolean }> { return req("/setup"); },
  async setup(username: string, password: string) { _csrf = null; return _postJson("/setup", { username, password }); },
  async login(username: string, password: string) { _csrf = null; return _postJson("/login", { username, password }); },
  async logout() { _csrf = null; return req("/logout", { method: "POST" }); },
  changePassword(current_password: string, new_password: string) { return mutate("POST", "/password", { current_password, new_password }); },
  getStatus(): Promise<Status> { return req("/status"); },
  getTrafficHistory(windowSec = 3600, maxPoints = 1200): Promise<TrafficHistoryResp> {
    return req(`/traffic/history?window_sec=${windowSec}&max_points=${maxPoints}`);
  },

  listNodes(): Promise<Node[]> { return req("/nodes"); },
  addNode(n: NodeIn): Promise<Node> { return mutate("POST", "/nodes", n); },
  updateNode(id: number, patch: NodeUpdate): Promise<Node> { return mutate("PATCH", `/nodes/${id}`, patch); },
  deleteNode(id: number) { return mutate("DELETE", `/nodes/${id}`); },
  apply(id: number) { return mutate("POST", `/nodes/${id}/apply`); },
  disconnect(id: number) { return mutate("POST", `/nodes/${id}/disconnect`); },
  rollback() { return mutate("POST", "/rollback"); },
  xrayStart() { return mutate("POST", "/xray/start"); },
  xrayStop() { return mutate("POST", "/xray/stop"); },

  listSubs(): Promise<Subscription[]> { return req("/subs"); },
  addSub(s: SubscriptionIn): Promise<Subscription> { return mutate("POST", "/subs", s); },
  updateSub(id: number, patch: Partial<SubscriptionIn>): Promise<Subscription> { return mutate("PATCH", `/subs/${id}`, patch); },
  deleteSub(id: number) { return mutate("DELETE", `/subs/${id}`); },
  refreshSub(id: number): Promise<any> { return mutate("POST", `/subs/${id}/refresh`); },
  refreshAllSubs(): Promise<{ refreshed: number }> { return mutate("POST", "/subs/refresh-all"); },
  previewSub(url: string, injection?: Record<string, any>): Promise<Preview> { return mutate("POST", "/subs/preview", { url, injection }); },
  previewSubNodes(url: string, injection?: Record<string, any>): Promise<PreviewNodes> { return mutate("POST", "/subs/preview-nodes", { url, injection }); },
  reorderNodes(ids: number[]) { return mutate("POST", "/nodes/reorder", { ids }); },
  connectBest(subscription_id: number | null): Promise<{ ok: boolean; node_id: number }> { return mutate("POST", "/connect-best", { subscription_id }); },
  importNodes(text: string): Promise<{ added: number; total: number; format: string }> { return mutate("POST", "/nodes/import", { text }); },

  getSettings(): Promise<Settings> { return req("/settings"); },
  putSettings(patch: Partial<Settings>): Promise<Settings> { return mutate("PUT", "/settings", patch); },
  resetSettings(): Promise<Settings> { return mutate("POST", "/settings/reset"); },
  getDiagnostics(): Promise<Diagnostics> { return req("/diagnostics"); },

  listProfiles(): Promise<TuningProfile[]> { return req("/profiles"); },
  addProfile(p: ProfileIn): Promise<TuningProfile> { return mutate("POST", "/profiles", p); },
  updateProfile(id: number, patch: ProfileUpdate): Promise<TuningProfile> { return mutate("PATCH", `/profiles/${id}`, patch); },
  deleteProfile(id: number) { return mutate("DELETE", `/profiles/${id}`); },
  setDefaultProfile(id: number): Promise<TuningProfile> { return mutate("PUT", "/profiles/default", { id }); },
  validateProfile(p: ProfileIn): Promise<{ ok: boolean; error: string }> { return mutate("POST", "/profiles/validate", p); },
  listProfilePresets(): Promise<ProfilePreset[]> { return req("/profiles/presets"); },
  applyProfileActive(id: number): Promise<{ ok: boolean; node_id: number }> { return mutate("POST", `/profiles/${id}/apply-active`); },

  getRouting(): Promise<Routing> { return req("/routing"); },
  putRouting(r: RoutingIn): Promise<Routing> { return mutate("PUT", "/routing", r); },
  listRoutingPresets(): Promise<PresetInfo[]> { return req("/routing/presets"); },
  routingPreset(name: string): Promise<Routing> { return mutate("POST", `/routing/preset/${encodeURIComponent(name)}`); },
  validateRouting(r: RoutingIn): Promise<{ ok: boolean; error: string }> { return mutate("POST", "/routing/validate", r); },

  listNodeHealth(): Promise<NodeHealth[]> { return req("/node-health"); },
  probeTcp(scope?: string): Promise<NodeHealth[]> { return mutate("POST", `/probe/tcp${scope ? `?scope=${encodeURIComponent(scope)}` : ""}`); },
  probeHttp(scope?: string): Promise<NodeHealth[]> { return mutate("POST", `/probe/http${scope ? `?scope=${encodeURIComponent(scope)}` : ""}`); },
  probeNode(id: number): Promise<NodeHealth> { return mutate("POST", `/nodes/${id}/probe`); },
  detachNodes(ids: number[]) { return mutate("POST", "/nodes/detach", { ids }); },
  validateNode(n: NodeIn): Promise<{ ok: boolean; error: string }> { return mutate("POST", "/nodes/validate", n); },

  getNetwork(): Promise<Network> { return req("/network"); },
  putNetwork(patch: NetworkPatch): Promise<Network> { return mutate("PUT", "/network", patch); },

  getBackup(): Promise<BackupDoc> { return req("/backup"); },
  restore(doc: BackupDoc): Promise<any> { return mutate("POST", "/restore", doc); },
  getLogs(source: string, lines = 200): Promise<{ source: string; lines: string[] }> {
    return req(`/logs?source=${encodeURIComponent(source)}&lines=${lines}`);
  },

  // live traffic WebSocket (session cookie authenticates the handshake). Returns the
  // socket so the caller owns reconnect/close. onMessage gets each parsed frame.
  openTraffic(onMessage: (m: TrafficMessage) => void): WebSocket {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/api/ws/traffic`);
    ws.onmessage = (e) => onMessage(JSON.parse(e.data));
    return ws;
  },
};
