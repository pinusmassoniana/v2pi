export interface Status {
  running: boolean; pid: number | null; active_node_id: number | null; xray_state: string;
  active_since: number | null; last_failover_at: number | null; prev_active_node_id: number | null; server_now: number;
  // Would POST /rollback succeed? Not implied by prev_active_node_id, which only names the node: a revocation
  // drops the snapshot pairing on purpose, so a rollback can be refused while a previous node is still recorded.
  rollback_available?: boolean;
  // Is the RUNNING xray serving the config on disk? "drift" = it is not (the file was rewritten
  // and nothing reloaded it, so a revoked client can still be admitted). "unknown" is its own
  // answer — nothing started yet, the normal state at boot — and must never render as a problem.
  config_drift?: "ok" | "drift" | "unknown";
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
  /** What the last refresh dropped, per protocol ({ trojan: 3 }). Keys come from a fixed
   *  backend vocabulary, never straight from the feed. */
  last_skipped: SkippedEntries;
}
/** Panel-supported labels plus "invalid" (unusable entry) and "other" (protocol with no name here). */
export type SkippedEntries = Record<string, number>;
export interface SubscriptionIn {
  name: string; url: string; interval_sec?: number; injection?: Record<string, any>;
  enabled?: boolean; default_profile_id?: number | null;
}
export interface Preview { method: string; url: string; headers: Record<string, string>; query: Record<string, string>; }
export interface PreviewNode { name: string; address: string; port: number; transport: string; network: string; security: string; }
export interface PreviewNodes { format: string; count: number; returned_count: number; truncated: boolean; nodes: PreviewNode[]; skipped: SkippedEntries; }
export interface RefreshResult { id?: number; name?: string; ok?: boolean; status?: string; error?: string | null; }
export interface RefreshAllResult { attempted: number; succeeded: number; failed: number; results: RefreshResult[] | Record<string, RefreshResult>; }
export interface Settings {
  tunneled_fetch: boolean;
  // Off by default protection would be nicer, but the server currently defaults this ON: a
  // scheduled subscription refresh may replace the LIVE active node with no operator action.
  subs_auto_switch: boolean;
  routing_default_action: string;
  // health_enabled is the master switch; the two loops below it are separately controllable.
  health_enabled: boolean; health_sweep_enabled: boolean;
  health_interval: number;          // all-nodes sweep cadence (s)
  health_active_interval: number;   // active-node real-check cadence (s)
  health_hysteresis: number; health_probe_url: string;
  failover_enabled: boolean; failover_cooldown: number;
  stats_enabled: boolean; stats_api_port: number; traffic_sample_ms: number;
  dns_intercept: boolean; session_timeout_min: number; auto_backup_enabled: boolean;
  /** B2: the daily release check. Off means the gateway never calls GitHub on its own. */
  update_check_enabled: boolean;
}
/**
 * The settings PUT /settings re-applies the live tunnel for (routes.py _SETTINGS_CONFIG_KEYS), inside the same
 * transaction: a patch holding one waits on a rebuild, `xray -test` and a reload, and a 502 rolls the whole patch back.
 */
export const SETTINGS_REAPPLY_KEYS = ["tunneled_fetch", "dns_intercept", "stats_enabled", "stats_api_port"] as const satisfies readonly (keyof Settings)[];
export interface Diagnostics {
  app_version: string; xray_version: string; uptime_sec: number;
  db_path: string; db_bytes: number; disk_free_bytes: number; disk_total_bytes: number;
  // The xray StatsService client's own health (api/schemas.py DiagnosticsOut), the only place the
  // panel can say why the Home graph is flat: when the last sample succeeded (null = never),
  // the last error text, and how many reads have failed consecutively since the last success or
  // port change (stats/client.py resets fail_count to 0 on both — it is never a since-boot total).
  stats_last_ok_at: number | null; stats_error: string; stats_fail_count: number;
  // B2: the newest published releases as of the last check, whether they are newer than what is
  // running (compared on the gateway, which owns the version scheme), when the check last ran
  // and why it failed. All empty until a check succeeds; an offline box is not an error.
  latest_app_version: string; latest_xray_version: string;
  app_update_available: boolean; xray_update_available: boolean;
  update_checked_at: number | null; update_error: string; update_check_enabled: boolean;
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
export interface RoutingRule { id: number; position: number; type: string; value: string; action: string; enabled: boolean; label: string; dataset: string; }
export interface RoutingRuleIn { type: string; value: string; action: string; enabled?: boolean; label?: string; dataset?: string; }
export interface Routing { rules: RoutingRule[]; default_action: string; domain_strategy: string; }
export interface RoutingIn { rules: RoutingRuleIn[]; default_action: string; domain_strategy?: string; }
export interface PresetInfo { name: string; title: string; dataset: string; default_action: string | null; }

// --- A4/B1: pinned devices (DHCP reservations) and what each moved ---
export interface Reservation {
  id: number; mac: string; ip: string; name: string; created_at: number;
  /** Bytes in the window the list reports (`window_sec`); 0 until the sampler has seen traffic. */
  up_bytes: number; down_bytes: number;
  /** Whether the segment's lease file shows it right now — pinned but switched off is normal. */
  online: boolean;
}
export interface Reservations { reservations: Reservation[]; window_sec: number; max_reservations: number; }

// --- A3: the geo data files the gateway routes by ---
export interface GeoFile {
  dataset: string; file: string; source: string;
  present: boolean; bytes: number; updated_at: number | null; has_previous: boolean;
}
export interface Geo { files: GeoFile[]; asset_dir: string; disk_free_bytes: number; }
export interface GeoUpdate { ok: boolean; dataset: string; files: string[]; reloaded: boolean; error: string; geo: Geo; }

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
  // Why host enforcement is not confirmed, when a network operation failed ("" otherwise).
  enforcement_error: string;
  // The last apply SUCCEEDED except for a secondary part (LAN access). Render it as a
  // warning, never as a failure — a partially-applied network is still enforcing.
  enforcement_warning?: string;
}
export interface RouterRec { title: string; detail: string; }
export interface ConnEvent { ts: number; kind: string; detail: string; }
// --- road-warrior inbound (reach the gateway + its LAN from outside) ---
export interface RwClient { id: string; email: string; enabled: boolean; }
// How a revocation reached the running xray. "cleaned" and "not-live" are NOT interchangeable:
// "cleaned" means xray is down and the stored config was rewritten, so the credential cannot
// come back on the next start; "not-live" means the config carried no inbound at all and
// nothing was written. Reporting the first as the second told operators that a completed
// revocation had cut nothing.
//
// "stopped" and "stop-failed" split the same way and matter more. "stopped" is a confirmed
// shutdown — nothing is being served. "stop-failed" means the fail-safe stop ran and could not
// be confirmed (xray survived SIGKILL): the process is still up on the config it loaded, and the
// REVOKED CREDENTIAL MAY STILL BE LIVE. It reported as "stopped", so the screen told an operator
// who had just lost a phone that remote access was down while the device could still connect.
// Anything rendering this value must treat "stop-failed" as a security warning.
export type RwRevocation =
  "" | "reapplied" | "rebuilt" | "cleaned" | "stopped" | "stop-failed" | "not-live";
export interface Rw {
  enabled: boolean; port: number; dest: string; server_names: string; short_ids: string;
  public_key: string; endpoint: string;
  // The Reality private key is never sent to the browser — only whether one is stored.
  has_private_key: boolean;
  hosts: Record<string, string>;
  // Non-empty when the STORED state is malformed (hand-edited DB / foreign backup). The screen
  // stays usable and shows what is wrong rather than failing to load.
  state_error: string;
  routed_nets: string[];         // effective list (derived from the net plan unless overridden)
  routed_nets_override: string;  // raw csv override, "" when deriving
  clients: RwClient[];
  // Whether remote access is being served right now: xray is not affirmatively stopped and the
  // config it loaded carries the inbound (schemas.py RwOut.live). It does NOT mean these settings
  // are the running ones — a save with no active node is stored and applied on the next connect.
  live: boolean;
  // How a revocation (client suspended/deleted, feature switched off, key rotated) reached the
  // running xray. Never empty on a revocation — see backend RwOut for the full contract.
  revocation: RwRevocation;
  // A committed narrowing has not been proven to reach the running xray yet: the revoked
  // credential may still be accepted. A security warning; it survives reloads and restarts.
  revocation_pending: boolean;
}
// PUT /rw is a FULL replace: every field it omits resets to its default, so every field is sent.
// private_key "" keeps the stored key (the browser never receives it).
export interface RwIn {
  enabled: boolean; port: number; dest: string; server_names: string; short_ids: string;
  public_key: string; endpoint: string; private_key: string;
  hosts: Record<string, string>; routed_nets: string;
}
export interface RwClientConfig { filename: string; config: string; }

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

/**
 * One audit row: every `/api` POST / PUT / PATCH / DELETE the middleware saw, whatever its outcome —
 * 2xx, 4xx, 5xx and its own 413, which is raised before the session is even checked (app.py). The
 * route's docstring still says "successful mutations" and is wrong. `path` never carries the query
 * string, but it does carry a remote-access client uuid on /api/rw/clients/‹id›.
 */
export interface AuditEntry { ts: number; actor: string; method: string; path: string; status: number; }

/**
 * POST /restore's reply (routes.py post_restore; `restored` is backup/__init__.py's summary). A restore
 * always leaves the gateway disconnected — xray stopped, no active node, fail-closed guard — even when it
 * succeeds, so `runtime` has one value. `rw_disabled` is empty unless the restore had to turn remote access
 * off, and `pre_restore_snapshot` is an absolute path on the gateway that no route reads back.
 */
export interface RestoreResult {
  ok: true;
  restored: { nodes: number; subscriptions: number; profiles: number; routing_rules: number; rw_disabled: string };
  runtime: "disconnected";
  pre_restore_snapshot: string;
}

function formatApiDetail(detail: unknown, fallback: string): string {
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

/**
 * No HTTP response arrived — the request timed out or the network failed. A write may still have committed on the
 * gateway (it keeps working under its lock), so it is neither a success nor a refusal: re-read and see.
 */
export function isNoAnswer(error: unknown): boolean {
  return error instanceof ApiError && error.status === 0;
}

// Shared fallback-message extractor — was pasted across eight call sites; every screen now
// imports this instead of redefining it.
export function errText(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
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
/** A write that re-applies the live tunnel under the store lock: rebuild, `xray -test` (15 s), reload and net apply. */
export const REAPPLY_TIMEOUT_MS = 60_000;
/** PUT /network re-provisions the host under the store lock: host commands, dnsmasq and xray checks, maybe a second pass. */
export const NETWORK_APPLY_TIMEOUT_MS = 180_000;

function reappliesTunnel(patch: Partial<Settings>): boolean {
  return SETTINGS_REAPPLY_KEYS.some((key) => key in patch);
}

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

// A POST that computes an answer without changing anything on the server — preview, validate, or
// stage a preset into an editor. It needs the CSRF token like a write; callers must not invalidate
// queries after it, or the refetch would overwrite the form the reply was staged into.
function peek(path: string, body?: unknown, timeoutMs?: number): Promise<any> {
  return mutate("POST", path, body, timeoutMs);
}

function _postJson(path: string, body: unknown, extraHeaders: Record<string, string> = {}) {
  return req(path, { method: "POST", headers: { "Content-Type": "application/json", ...extraHeaders }, body: JSON.stringify(body) });
}

export const TRAFFIC_CAPABILITY_EVENT = "v2pi:traffic-capability-change";

// The write may have changed the stats settings: a traffic stream parked on "disabled" checks again.
function announceCapabilityChange<T>(result: T): T {
  if (typeof document !== "undefined") document.dispatchEvent(new Event(TRAFFIC_CAPABILITY_EVENT));
  return result;
}

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
  previewSub(url: string, injection?: Record<string, any>): Promise<Preview> { return peek("/subs/preview", { url, injection }); },
  // The backend's own fetch budgets 20 s; give the round-trip a longer client timeout so a slow-but-alive
  // provider isn't aborted out from under it. Callers that want the default may omit timeoutMs.
  previewSubNodes(url: string, injection?: Record<string, any>, timeoutMs?: number): Promise<PreviewNodes> {
    return peek("/subs/preview-nodes", { url, injection }, timeoutMs);
  },
  reorderNodes(ids: number[]) { return mutate("POST", "/nodes/reorder", { ids }); },
  connectBest(subscription_id: number | null): Promise<{ ok: boolean; node_id: number }> { return mutate("POST", "/connect-best", { subscription_id }); },
  importNodes(text: string): Promise<{ added: number; total: number; format: string; skipped: SkippedEntries }> { return mutate("POST", "/nodes/import", { text }); },

  getSettings(): Promise<Settings> { return req("/settings"); },
  putSettings(patch: Partial<Settings>): Promise<Settings> { return mutate("PUT", "/settings", patch, reappliesTunnel(patch) ? REAPPLY_TIMEOUT_MS : undefined).then(announceCapabilityChange); },
  // A reset writes every re-apply key back to its default, so it always re-applies.
  resetSettings(): Promise<Settings> { return mutate("POST", "/settings/reset", undefined, REAPPLY_TIMEOUT_MS).then(announceCapabilityChange); },
  getDiagnostics(): Promise<Diagnostics> { return req("/diagnostics"); },

  listReservations(): Promise<Reservations> { return req("/net/reservations"); },
  /** Pins one device's address: re-renders dnsmasq (which restarts) and the nft ruleset. */
  addReservation(mac: string, ip: string, name: string): Promise<Reservations> { return mutate("POST", "/net/reservations", { mac, ip, name }); },
  renameReservation(id: number, name: string): Promise<Reservations> { return mutate("PATCH", `/net/reservations/${id}`, { name }); },
  deleteReservation(id: number): Promise<Reservations> { return mutate("DELETE", `/net/reservations/${id}`); },

  getGeo(): Promise<Geo> { return req("/geo"); },
  /** Replaces one dataset and reloads xray: a short drop for everyone behind the gateway. */
  updateGeo(dataset: string): Promise<GeoUpdate> { return mutate("POST", "/geo/update", { dataset }, REAPPLY_TIMEOUT_MS); },
  revertGeo(dataset: string): Promise<GeoUpdate> { return mutate("POST", "/geo/revert", { dataset }, REAPPLY_TIMEOUT_MS); },
  /** B2: run the release check now. It reports; nothing here installs anything. */
  checkUpdates(): Promise<Diagnostics> { return mutate("POST", "/updates/check"); },

  listProfiles(): Promise<TuningProfile[]> { return req("/profiles"); },
  addProfile(p: ProfileIn): Promise<TuningProfile> { return mutate("POST", "/profiles", p); },
  updateProfile(id: number, patch: ProfileUpdate): Promise<TuningProfile> { return mutate("PATCH", `/profiles/${id}`, patch); },
  deleteProfile(id: number) { return mutate("DELETE", `/profiles/${id}`); },
  setDefaultProfile(id: number): Promise<TuningProfile> { return mutate("PUT", "/profiles/default", { id }); },
  validateProfile(p: ProfileIn): Promise<{ ok: boolean; error: string }> { return peek("/profiles/validate", p); },
  listProfilePresets(): Promise<ProfilePreset[]> { return req("/profiles/presets"); },
  applyProfileActive(id: number): Promise<{ ok: boolean; node_id: number }> { return mutate("POST", `/profiles/${id}/apply-active`); },

  getRouting(): Promise<Routing> { return req("/routing"); },
  putRouting(r: RoutingIn): Promise<Routing> { return mutate("PUT", "/routing", r); },
  listRoutingPresets(): Promise<PresetInfo[]> { return req("/routing/presets"); },
  routingPreset(name: string): Promise<Routing> { return peek(`/routing/preset/${encodeURIComponent(name)}`); },
  validateRouting(r: RoutingIn): Promise<{ ok: boolean; error: string }> { return peek("/routing/validate", r); },

  listNodeHealth(): Promise<NodeHealth[]> { return req("/node-health"); },
  probeTcp(scope?: string): Promise<NodeHealth[]> { return mutate("POST", `/probe/tcp${scope ? `?scope=${encodeURIComponent(scope)}` : ""}`, undefined, PROBE_SWEEP_TIMEOUT_MS); },
  probeHttp(scope?: string): Promise<NodeHealth[]> { return mutate("POST", `/probe/http${scope ? `?scope=${encodeURIComponent(scope)}` : ""}`, undefined, PROBE_SWEEP_TIMEOUT_MS); },
  probeNode(id: number): Promise<NodeHealth> { return mutate("POST", `/nodes/${id}/probe`); },
  detachNodes(ids: number[]) { return mutate("POST", "/nodes/detach", { ids }); },
  validateNode(n: NodeValidateIn): Promise<{ ok: boolean; error: string }> { return peek("/nodes/validate", n); },

  getNetwork(signal?: AbortSignal): Promise<Network> { return req("/network", { signal }); },
  putNetwork(patch: NetworkPatch): Promise<Network> { return mutate("PUT", "/network", patch, NETWORK_APPLY_TIMEOUT_MS); },

  getRw(): Promise<Rw> { return req("/rw"); },
  putRw(body: RwIn): Promise<Rw> { return mutate("PUT", "/rw", body, REAPPLY_TIMEOUT_MS); },
  addRwClient(email: string): Promise<Rw> { return mutate("POST", "/rw/clients", { email }, REAPPLY_TIMEOUT_MS); },
  setRwClientEnabled(id: string, enabled: boolean): Promise<Rw> { return mutate("PATCH", `/rw/clients/${encodeURIComponent(id)}`, { enabled }, REAPPLY_TIMEOUT_MS); },
  newRwShortId(): Promise<{ short_id: string }> { return peek("/rw/short-id"); },
  deleteRwClient(id: string): Promise<Rw> { return mutate("DELETE", `/rw/clients/${encodeURIComponent(id)}`, undefined, REAPPLY_TIMEOUT_MS); },
  rwClientLink(id: string): Promise<{ link: string }> { return req(`/rw/clients/${encodeURIComponent(id)}/link`); },
  rwClientConfig(id: string): Promise<RwClientConfig> { return req(`/rw/clients/${encodeURIComponent(id)}/config`); },

  listTokens(): Promise<ApiToken[]> { return req("/tokens"); },
  // expires_at is OMITTED when there is none, never sent as null: TokenCreateIn is a StrictIn whose
  // expires_at is `int | None` with ge=1, so 0 and null are both refused by the schema.
  createToken(name: string, scope: ApiTokenScope, expiresAt?: number): Promise<ApiTokenCreated> { return mutate("POST", "/tokens", { name, scope, ...(expiresAt ? { expires_at: expiresAt } : {}) }); },
  deleteToken(id: number) { return mutate("DELETE", `/tokens/${id}`); },

  listAudit(limit = 100): Promise<AuditEntry[]> { return req(`/audit?limit=${limit}`); },

  getBackup(): Promise<BackupDoc> { return req("/backup"); },
  // The handler validates, snapshots, stops xray, installs the fail-closed guard and runs a full
  // host_provision under apply_lock — the longest write in the app, so it gets the host-apply timeout.
  restore(doc: BackupDoc): Promise<RestoreResult> { return mutate("POST", "/restore", doc, NETWORK_APPLY_TIMEOUT_MS).then(announceCapabilityChange); },
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
