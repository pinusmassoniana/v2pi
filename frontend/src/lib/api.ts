export interface Status {
  running: boolean; pid: number | null; active_node_id: number | null; xray_state: string;
  active_since: number | null; last_failover_at: number | null; prev_active_node_id: number | null; server_now: number;
  tunnel_online?: boolean | null; failover_ready?: boolean; eligible_standby_count?: number;
  active_health_fresh?: boolean; health_enabled?: boolean; failover_enabled?: boolean; failovers_24h?: number;
}
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
export type NodeValidateIn = NodeIn & { tuning_profile_id?: number | null };

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
export interface PreviewNodes { format: string; count: number; returned_count: number; truncated: boolean; nodes: PreviewNode[]; }
export interface RefreshResult { id?: number; name?: string; ok?: boolean; status?: string; error?: string | null; }
export interface RefreshAllResult { attempted: number; succeeded: number; failed: number; results: RefreshResult[] | Record<string, RefreshResult>; }
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
  active: { node_id: number; real_ok: boolean | null; stale: boolean; latency_ms: number | null; egress_ip: string | null; egress_ip6: string | null; egress_cc: string | null; egress_cc6: string | null; checked_at: string | null; lat_history: number[] } | null;
}
export type TrafficMessage = TrafficFrame | { disabled: true } | { error: string };
// long-window history seed: each sample is [ts_ms, up_bps, down_bps]
export interface TrafficHistoryResp { samples: number[][]; interval_ms: number; effective_interval_ms?: number; }
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
  clients: DhcpClient[]; tunnel: NetworkTunnel; wan_blocked: boolean | null;
  ipv6_prefix: string | null;   // DHCPv6-PD 'auto': host-delegated segment v6 prefix
  foreign_ra: boolean | null;   // another router advertising v6 on the segment (leak)
  ipv6_prefix_source: string | null;   // "static" | "ula" | "pd"
  enforcement_status?: "ok" | "unknown" | "error";
  failovers_24h?: number; failover_ready?: boolean; eligible_standby_count?: number;
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

// API tokens (programmatic REST access). `token` (the full secret) is returned ONLY by createToken.
export type ApiTokenScope = "monitor" | "read" | "readwrite";
export interface ApiToken { id: number; name: string; scope: ApiTokenScope; prefix: string; created_at: number; last_used_at: number | null; expires_at?: number | null; }
export interface ApiTokenCreated extends ApiToken { token: string; }

// audit log (N2): successful mutations — who/what/when
export interface AuditEntry { ts: number; actor: string; method: string; path: string; status: number; }

export function formatApiDetail(detail: unknown, fallback: string): string {
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) {
    const lines = detail.map((item) => {
      if (!item || typeof item !== "object") return String(item);
      const row = item as { loc?: unknown; msg?: unknown };
      const path = Array.isArray(row.loc) ? row.loc.map(String).join(".") : "request";
      return `${path}: ${typeof row.msg === "string" ? row.msg : "invalid value"}`;
    });
    if (lines.length) return lines.join("\n");
  }
  return fallback;
}

export class ApiError extends Error {
  constructor(public status: number, msg: string, public detail: unknown = msg) { super(msg); }
}

let _csrf: string | null = null;
let _csrfInflight: Promise<string> | null = null;   // dedup concurrent /csrf fetches
// F1: session died mid-use (expiry / password change elsewhere) — the app shell registers a
// handler that drops back to the Login screen instead of leaving dead panels around.
let _onUnauthorized: (() => void) | null = null;
export function setOnUnauthorized(fn: (() => void) | null) { _onUnauthorized = fn; }

// Bound every request so a stalled TCP connection (radio switch, VPN reconnect on mobile) fails
// fast instead of leaving the promise pending forever. Generous enough for a real-probe sweep.
const REQUEST_TIMEOUT_MS = 20000;
const PROBE_SWEEP_TIMEOUT_MS = 210000;

async function req(path: string, opts: RequestInit = {}, timeoutMs = REQUEST_TIMEOUT_MS): Promise<any> {
  const ac = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; ac.abort(); }, timeoutMs);
  // honour a caller-supplied signal too (abort either way)
  const ext = opts.signal;
  if (ext) {
    if (ext.aborted) ac.abort();
    else ext.addEventListener("abort", () => ac.abort(), { once: true });
  }
  let res: Response;
  try {
    res = await fetch(`/api${path}`, { credentials: "include", ...opts, signal: ac.signal });
  } catch (e) {
    if (timedOut) throw new ApiError(0, "request timed out");
    throw new ApiError(0, "network error");
  } finally {
    clearTimeout(timer);
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (res.status === 401) {
      _csrf = null;                              // the CSRF token died with the session
      if (path !== "/login") _onUnauthorized?.(); // a failed login is not a lost session
    }
    const detail = body?.detail;
    throw new ApiError(res.status, formatApiDetail(detail, `HTTP ${res.status}`), detail);
  }
  return body;
}

async function ensureCsrf(): Promise<string> {
  if (_csrf) return _csrf;
  // dedup: a burst of mutations with no token yet share one /csrf round-trip
  if (!_csrfInflight) {
    _csrfInflight = req("/csrf")
      .then((r) => { _csrf = r.csrf; return r.csrf as string; })
      .finally(() => { _csrfInflight = null; });
  }
  return _csrfInflight;
}

async function mutate(method: string, path: string, body?: unknown, timeoutMs?: number): Promise<any> {
  const headers: Record<string, string> = { "X-CSRF-Token": await ensureCsrf() };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  return req(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) }, timeoutMs);
}

function _postJson(path: string, body: unknown, extraHeaders: Record<string, string> = {}) {
  return req(path, { method: "POST", headers: { "Content-Type": "application/json", ...extraHeaders }, body: JSON.stringify(body) });
}

export interface LatestRequest {
  run<T>(request: (signal: AbortSignal) => Promise<T>, apply: (value: T) => void): Promise<void>;
  cancel(): void;
}

export function createLatestRequest(): LatestRequest {
  let controller: AbortController | null = null;
  let sequence = 0;
  return {
    async run<T>(request: (signal: AbortSignal) => Promise<T>, apply: (value: T) => void) {
      controller?.abort();
      const current = ++sequence;
      const own = new AbortController();
      controller = own;
      try {
        const value = await request(own.signal);
        if (current === sequence) apply(value);
      } finally {
        if (current === sequence) controller = null;
      }
    },
    cancel() { sequence++; controller?.abort(); controller = null; },
  };
}

export const TRAFFIC_CAPABILITY_EVENT = "v2pi:traffic-capability-change";

export const api = {
  _reset() { _csrf = null; _csrfInflight = null; },
  ensureCsrf,
  // first-run setup: creates the credential AND opens a session (no prior auth needed)
  getSetup(): Promise<{ needs_setup: boolean; bootstrap_required?: boolean }> { return req("/setup"); },
  async setup(username: string, password: string, bootstrapToken = "") {
    _csrf = null;
    return _postJson("/setup", { username, password }, bootstrapToken ? { "X-Bootstrap-Token": bootstrapToken } : {});
  },
  async login(username: string, password: string) { _csrf = null; return _postJson("/login", { username, password }); },
  async logout() {
    // send the CSRF header (logout is now CSRF-protected) THEN clear the cached token
    try { return await mutate("POST", "/logout"); }
    finally { _csrf = null; _csrfInflight = null; }
  },
  changePassword(current_password: string, new_password: string) { return mutate("POST", "/password", { current_password, new_password }); },
  getStatus(signal?: AbortSignal): Promise<Status> { return req("/status", { signal }); },
  getTrafficHistory(windowSec = 3600, maxPoints = 1200, signal?: AbortSignal): Promise<TrafficHistoryResp> {
    return req(`/traffic/history?window_sec=${windowSec}&max_points=${maxPoints}`, { signal });
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
  refreshAllSubs(): Promise<RefreshAllResult> { return mutate("POST", "/subs/refresh-all"); },
  previewSub(url: string, injection?: Record<string, any>): Promise<Preview> { return mutate("POST", "/subs/preview", { url, injection }); },
  previewSubNodes(url: string, injection?: Record<string, any>): Promise<PreviewNodes> { return mutate("POST", "/subs/preview-nodes", { url, injection }); },
  reorderNodes(ids: number[]) { return mutate("POST", "/nodes/reorder", { ids }); },
  connectBest(subscription_id: number | null): Promise<{ ok: boolean; node_id: number }> { return mutate("POST", "/connect-best", { subscription_id }); },
  importNodes(text: string): Promise<{ added: number; total: number; format: string }> { return mutate("POST", "/nodes/import", { text }); },

  getSettings(): Promise<Settings> { return req("/settings"); },
  async putSettings(patch: Partial<Settings>): Promise<Settings> {
    const result = await mutate("PUT", "/settings", patch);
    if (typeof document !== "undefined") document.dispatchEvent(new Event(TRAFFIC_CAPABILITY_EVENT));
    return result;
  },
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
  probeTcp(scope?: string): Promise<NodeHealth[]> { return mutate("POST", `/probe/tcp${scope ? `?scope=${encodeURIComponent(scope)}` : ""}`, undefined, PROBE_SWEEP_TIMEOUT_MS); },
  probeHttp(scope?: string): Promise<NodeHealth[]> { return mutate("POST", `/probe/http${scope ? `?scope=${encodeURIComponent(scope)}` : ""}`, undefined, PROBE_SWEEP_TIMEOUT_MS); },
  probeNode(id: number): Promise<NodeHealth> { return mutate("POST", `/nodes/${id}/probe`); },
  detachNodes(ids: number[]) { return mutate("POST", "/nodes/detach", { ids }); },
  validateNode(n: NodeValidateIn): Promise<{ ok: boolean; error: string }> { return mutate("POST", "/nodes/validate", n); },

  getNetwork(): Promise<Network> { return req("/network"); },
  putNetwork(patch: NetworkPatch): Promise<Network> { return mutate("PUT", "/network", patch); },

  listTokens(): Promise<ApiToken[]> { return req("/tokens"); },
  createToken(name: string, scope: ApiTokenScope): Promise<ApiTokenCreated> { return mutate("POST", "/tokens", { name, scope }); },
  deleteToken(id: number) { return mutate("DELETE", `/tokens/${id}`); },

  listAudit(limit = 100): Promise<AuditEntry[]> { return req(`/audit?limit=${limit}`); },

  getBackup(): Promise<BackupDoc> { return req("/backup"); },
  restore(doc: BackupDoc): Promise<any> { return mutate("POST", "/restore", doc); },
  getLogs(source: string, lines = 200): Promise<{ source: string; lines: string[] }> {
    return req(`/logs?source=${encodeURIComponent(source)}&lines=${lines}`);
  },

  // live traffic WebSocket (session cookie authenticates the handshake). Returns the
  // socket so the caller owns reconnect/close. onMessage gets each parsed frame.
  openTraffic(onMessage: (m: TrafficMessage) => void): WebSocket {
    return _openTrafficSocket(onMessage);
  },

  // Preferred: a self-managing live-traffic connection. Reconnects with capped exponential
  // backoff, pauses while the tab is hidden (mobile battery), and calls onGap() before each
  // reconnect so callers can backfill the missed window. Returns a handle; call .close() to stop.
  connectTraffic(onMessage: (m: TrafficMessage) => void, onGap?: () => void): TrafficHandle {
    let ws: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let stop = false;
    let terminal = false;
    let retry = 0;
    const hidden = () => typeof document !== "undefined" && document.hidden;
    const schedule = () => {
      if (stop || timer || hidden()) return;   // paused when hidden; onVis resumes
      const delay = Math.min(15000, 1000 * 2 ** retry) + Math.random() * 400;
      retry++;
      timer = setTimeout(() => { timer = null; open(); }, delay);
    };
    const open = () => {
      if (stop || terminal || hidden()) return;
      const socket = _openTrafficSocket((message) => {
        if ("disabled" in message) terminal = true;
        onMessage(message);
      });
      ws = socket;
      socket.onopen = () => { retry = 0; };
      socket.onclose = () => {
        if (ws === socket) ws = null;
        else if (ws) return; // a hidden-tab close completed after its visible replacement opened
        if (!stop && !terminal && !hidden()) { onGap?.(); schedule(); }
      };
      socket.onerror = () => { try { socket.close(); } catch {} };
    };
    const resume = () => {
      if (stop) return;
      terminal = false;
      retry = 0;
      if (!hidden() && !ws) { onGap?.(); open(); }
    };
    const onVis = () => {
      if (hidden()) {
        if (timer) { clearTimeout(timer); timer = null; }
        const socket = ws; ws = null;
        try { socket?.close(); } catch {}
      } else if (!terminal && !ws && !stop) {
        retry = 0; onGap?.(); open();
      }
    };
    if (typeof document !== "undefined") document.addEventListener("visibilitychange", onVis);
    if (typeof document !== "undefined") document.addEventListener(TRAFFIC_CAPABILITY_EVENT, resume);
    open();
    return {
      resume,
      close() {
        stop = true;
        if (timer) clearTimeout(timer);
        if (typeof document !== "undefined") document.removeEventListener("visibilitychange", onVis);
        if (typeof document !== "undefined") document.removeEventListener(TRAFFIC_CAPABILITY_EVENT, resume);
        try { ws?.close(); } catch {}
        ws = null;
      },
    };
  },
};

export interface TrafficHandle { close(): void; resume(): void; }

function _openTrafficSocket(onMessage: (m: TrafficMessage) => void): WebSocket {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/api/ws/traffic`);
  // a single malformed frame must not throw out of the handler and break the stream
  ws.onmessage = (e) => {
    let m: TrafficMessage;
    try { m = JSON.parse(e.data); } catch { m = { error: "bad frame" }; }
    onMessage(m);
  };
  return ws;
}
