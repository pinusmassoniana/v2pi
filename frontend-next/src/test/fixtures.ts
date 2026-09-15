import type { QueryClient, QueryKey } from "@tanstack/react-query";
import { act } from "@testing-library/react";
import { vi } from "vitest";
import type {
  Network, Node, NodeHealth, NodeIn, NodeUpdate, PresetInfo, PreviewNodes, Preview, ProfileIn, ProfilePreset, ProfileUpdate, RefreshAllResult,
  Routing, RoutingIn, Settings, Status, Subscription, SubscriptionIn, TrafficFrame, TrafficMessage, TuningProfile,
} from "../api/client";
import { api } from "../api/client";
import { recordServerNow } from "../api/clock";
import { CONNECTION_WRITE } from "../api/invalidation";
import { trafficStore } from "../api/traffic";

/** Gateway time for every fixture: 2023-11-14 22:15:00 UTC, in epoch seconds. */
export const NOW_SEC = 1_700_000_100;
const iso = (epochSec: number) => new Date(epochSec * 1000).toISOString().replace(".000Z", "Z");

export const STATUS: Status = {
  running: true, pid: 7, active_node_id: 1, xray_state: "working", active_since: 1_700_000_000,
  last_failover_at: null, prev_active_node_id: null, rollback_available: true, server_now: NOW_SEC, tunnel_online: true,
  config_drift: "ok", failover_ready: true, eligible_standby_count: 3, active_health_fresh: true,
  health_enabled: true, failover_enabled: true, failovers_24h: 2,
};

export function node(id: number, name: string): Node {
  return {
    id, name, address: `${name}.example.org`, port: 443, uuid: `uuid-${id}`, transport: "vision",
    network: "tcp", security: "reality", sni: "", public_key: "", short_id: "", fingerprint: "chrome",
    path: "", host: "", mode: "", alpn: "", note: "", subscription_id: null, stale: false,
    tuning_profile_id: null,
  };
}

/**
 * The "work" subscription's six nodes: the active one (reality, with a tuning profile), three probed standbys (an
 * xhttp·reality one, and a slow, old xhttp·tls one), one never probed, one whose real check failed.
 */
export const NODES: Node[] = [
  { ...node(1, "nl-ams-03"), subscription_id: 1, sni: "www.microsoft.com", public_key: "Zm9vX3JlYWxpdHlfcHViX2tleQ", short_id: "6ba85179e3", tuning_profile_id: 2 },
  { ...node(2, "de-fra-01"), subscription_id: 1, note: "streaming backup" },
  { ...node(3, "fi-hel-02"), subscription_id: 1, transport: "xhttp", network: "xhttp", public_key: "cHVia2V5LWZpLWhlbA", path: "/xh", host: "cdn.example.net", mode: "auto" },
  { ...node(4, "pl-waw-01"), subscription_id: 1, port: 8443, transport: "xhttp", network: "xhttp", security: "tls", alpn: "h2,http/1.1" },
  { ...node(5, "ch-zrh-02"), subscription_id: 1 },
  { ...node(6, "se-sto-01"), subscription_id: 1 },
];

/** Manual servers (no subscription), in position order. */
export const SERVER_NODES: Node[] = [
  { ...node(7, "vps-hel"), address: "198.51.100.23", note: "own VPS", public_key: "dnBzLWhlbC1wdWI" },
  { ...node(8, "lab-lan"), address: "192.168.1.50", port: 8443, transport: "xhttp", network: "xhttp", security: "tls", note: "LAN test box" },
  { ...node(9, "kz-ala-01"), address: "5.34.180.9" },
];

/** The "home" subscription's one node: it vanished from the feed and was kept. */
export const HOME_NODES: Node[] = [{ ...node(10, "us-nyc-01"), subscription_id: 2, stale: true, transport: "xhttp", network: "xhttp", security: "tls" }];

/** Every group at once — work 6 · home 1 · old 0 · Servers 3. mockApi() serves NODES; mockNodeGroups() serves these. */
export const ALL_NODES: Node[] = [...NODES, ...SERVER_NODES, ...HOME_NODES];

function health(nodeId: number, patch: Partial<NodeHealth>): NodeHealth {
  return {
    node_id: nodeId, last_tcp_ok: true, last_tcp_ms: null, last_http_ok: true, last_http_ms: null,
    last_real_ok: true, last_real_ms: null, egress_ip: null, egress_ip6: null, egress_cc: null, egress_cc6: null,
    checked_at: null, fail_count: 0, lat_history: [], ...patch,
  };
}

/** Background-sweep results. ch-zrh-02 (id 5) has never been probed. */
export const NODE_HEALTH: NodeHealth[] = [
  health(1, { last_tcp_ms: 31, last_http_ms: 142, last_real_ms: 45, egress_ip: "185.107.56.21", egress_cc: "NL", checked_at: iso(NOW_SEC - 5), lat_history: [44, 45] }),
  health(2, { last_tcp_ms: 40, last_http_ms: 118, last_real_ms: 58, egress_ip: "91.198.10.4", egress_cc: "DE", checked_at: iso(NOW_SEC - 240) }),
  health(3, { last_tcp_ms: 52, last_http_ms: 131, last_real_ms: 71, egress_ip: "95.216.3.9", egress_cc: "FI", checked_at: iso(NOW_SEC - 360) }),
  health(4, { last_tcp_ms: 120, last_http_ms: 164, last_real_ms: 164, egress_ip: "51.68.22.7", egress_cc: "PL", checked_at: iso(NOW_SEC - 1_380) }),
  health(6, { last_tcp_ok: true, last_tcp_ms: 80, last_http_ms: 139, last_real_ok: false, last_real_ms: null, egress_cc: "SE", checked_at: iso(NOW_SEC - 720), fail_count: 3 }),
];

/** Health of the other groups' nodes: vps-hel probed, lab-lan TCP only (its HTTP check failed), us-nyc-01 three hours ago. */
export const ALL_NODE_HEALTH: NodeHealth[] = [
  ...NODE_HEALTH,
  health(7, { last_tcp_ms: 46, last_http_ms: 128, last_real_ms: 69, egress_ip: "198.51.100.23", egress_ip6: "2001:db8::23", egress_cc: "FI", egress_cc6: "FI", checked_at: iso(NOW_SEC - 420), lat_history: [70, 68, 69] }),
  health(8, { last_tcp_ms: 2, last_http_ok: false, last_real_ok: null, checked_at: iso(NOW_SEC - 540) }),
  health(10, { last_tcp_ms: 96, last_http_ms: 212, last_real_ms: 118, egress_ip: "23.105.160.4", egress_cc: "US", checked_at: iso(NOW_SEC - 10_800) }),
];

export const NETWORK: Network = {
  segment: {
    iface: "eth0.2", ip: "10.0.2.1", ip6: "", dhcp_start: "10.0.2.100", dhcp_end: "10.0.2.149",
    dhcp_lease: "12h", client_dns: "10.0.2.1", client_dns6: "",
  },
  kill_switch_enabled: true, lan_access_enabled: false, ipv6_enabled: true,
  status: {
    segment_up: true, uplink: true, uplink6: true, dhcp_clients: 12,
    clients: [
      { ip: "10.0.2.101", mac: "aa:bb:cc:00:00:01", hostname: "pixel-7", expiry: NOW_SEC + 40_000 },
      { ip: "10.0.2.102", mac: "aa:bb:cc:00:00:02", hostname: "macbook", expiry: NOW_SEC + 30_000 },
    ],
    tunnel: { real_ok: true, latency_ms: 42, egress_ip: "185.107.56.21", checked_at: iso(NOW_SEC - 5) },
    wan_blocked: false, ipv6_prefix: null, foreign_ra: false, ipv6_prefix_source: "static",
    enforcement_status: "ok", failovers_24h: 2, failover_ready: true, eligible_standby_count: 3,
  },
  recommendations: [{ title: "Hand out the gateway as the router", detail: "Set DHCP option 3 to 10.0.2.1." }],
  // Oldest first, as the backend appends them.
  events: [
    { ts: NOW_SEC - 86_000, kind: "apply", detail: "config applied for fi-hel-02" },
    { ts: NOW_SEC - 50_000, kind: "failover", detail: "fi-hel-02 → de-fra-01 — real check failed 3×" },
    { ts: NOW_SEC - 10_000, kind: "subscription", detail: "“work” refreshed — 6 nodes, 1 new" },
    { ts: NOW_SEC - 1_100, kind: "retry", detail: "real check retry on de-fra-01" },
    { ts: NOW_SEC - 600, kind: "failover", detail: "de-fra-01 → nl-ams-03 — real check failed 3×" },
    { ts: NOW_SEC - 200, kind: "health", detail: "nl-ams-03 real check 45 ms" },
    { ts: NOW_SEC - 50, kind: "leak", detail: "untunneled traffic 120 kbit/s" },
  ],
};

function sub(patch: Partial<Subscription> & Pick<Subscription, "id" | "name">): Subscription {
  return {
    url: `https://provider.example/${patch.name}`, injection: {}, interval_sec: 86_400, enabled: true,
    default_profile_id: null, last_fetched: iso(NOW_SEC - 10_000), last_status: "ok", last_path: null,
    last_error: null, up_bytes: null, down_bytes: null, total_bytes: null, expire_at: null, node_count: 3, ...patch,
  };
}

/**
 * "work" expires in two days and refreshes hourly through the tunnel; "home" is at 86 % of its allowance and its last
 * fetch failed; "old" is paused (never warns), expired, never refreshes on its own and has no nodes left.
 */
export const SUBS: Subscription[] = [
  sub({
    id: 1, name: "work", url: "https://sub.work-vpn.example/api/v1/client/subscribe?token=9f2c7a1e", interval_sec: 3_600,
    injection: { headers: { "x-device-os": "{device_os}", "user-agent": "v2pi/1.0" }, query: { type: "vless" } },
    expire_at: NOW_SEC + 2 * 86_400, up_bytes: 2e9, down_bytes: 18e9, total_bytes: 100e9,
    last_status: "ok: +0 ~6 -0", last_path: "tunnel", last_fetched: iso(NOW_SEC - 240), node_count: 6,
  }),
  sub({
    id: 2, name: "home", interval_sec: 21_600, default_profile_id: 2, up_bytes: 6e9, down_bytes: 80e9, total_bytes: 100e9,
    last_status: "error: fetch failed: timeout", last_path: "tunnel", last_error: "fetch failed: timeout", node_count: 1,
  }),
  sub({ id: 3, name: "old", enabled: false, interval_sec: 0, expire_at: NOW_SEC - 86_400, last_path: "direct", node_count: 0 }),
];

/** Two tuning profiles: the global default and the one nl-ams-03 uses. */
export const PROFILES: TuningProfile[] = [
  {
    id: 1, name: "balanced", fingerprint: "chrome", frag_enabled: false, frag_packets: "", frag_length: "", frag_interval: "",
    mux_enabled: false, doh_enabled: false, doh_url: "", quic: "", noise_enabled: false, noises: [], xhttp_padding: "",
    xmux_max_concurrency: "", xmux_max_connections: "", mux_concurrency: "", xudp_proxy_udp443: "", alpn: "", tls_min: "", tls_max: "",
    is_default: true, is_active: false, node_count: 9,
  },
  {
    id: 2, name: "fragment-tls", fingerprint: "firefox", frag_enabled: true, frag_packets: "tlshello", frag_length: "100-200", frag_interval: "10-20",
    mux_enabled: false, doh_enabled: false, doh_url: "", quic: "", noise_enabled: false, noises: [], xhttp_padding: "",
    xmux_max_concurrency: "", xmux_max_connections: "", mux_concurrency: "", xudp_proxy_udp443: "", alpn: "", tls_min: "", tls_max: "",
    is_default: false, is_active: true, node_count: 1,
  },
];

/** Gateway settings: both subscription toggles on, health and auto-failover armed. */
export const SETTINGS: Settings = {
  tunneled_fetch: true, subs_auto_switch: true, routing_default_action: "proxy",
  health_enabled: true, health_sweep_enabled: true, health_interval: 600, health_active_interval: 30,
  health_hysteresis: 3, health_probe_url: "https://www.gstatic.com/generate_204",
  failover_enabled: true, failover_cooldown: 300,
  stats_enabled: true, stats_api_port: 10085, traffic_sample_ms: 1000,
  dns_intercept: false, session_timeout_min: 60, auto_backup_enabled: true,
};

/** What previewSub answers for "work": the request it would send. */
export const PREVIEW: Preview = {
  method: "GET",
  url: "https://sub.work-vpn.example/api/v1/client/subscribe?token=9f2c7a1e&type=vless",
  headers: { "x-device-os": "linux", "user-agent": "v2pi/1.0" },
  query: { type: "vless" },
};

/** What previewSubNodes answers: a big feed, truncated to its first 200 of 214 nodes (three shown here). */
export const PREVIEW_NODES: PreviewNodes = {
  format: "base64/vless", count: 214, returned_count: 200, truncated: true,
  nodes: [
    { name: "fra-edge-01", address: "91.203.150.4", port: 443, transport: "vision", network: "tcp", security: "reality" },
    { name: "ams-edge-07", address: "45.83.141.9", port: 443, transport: "xhttp", network: "xhttp", security: "reality" },
    { name: "sto-edge-03", address: "185.195.72.8", port: 8443, transport: "xhttp", network: "xhttp", security: "tls" },
  ],
};

/** Refresh all over the two enabled subscriptions: work succeeds, home times out. */
export const REFRESH_ALL: RefreshAllResult = {
  attempted: 2, succeeded: 1, failed: 1,
  results: [
    { id: 1, name: "work", ok: true, status: "ok: +0 ~6 -0", error: null },
    { id: 2, name: "home", ok: false, status: "error: fetch failed: timeout", error: "fetch failed: timeout" },
  ],
};

/** Six rules; the fourth is disabled, so the first four enabled ones skip it. */
export const ROUTING: Routing = {
  default_action: "proxy",
  domain_strategy: "IPIfNonMatch",
  rules: [
    { id: 11, position: 1, type: "domain", value: "netflix.com", action: "proxy", enabled: true, label: "" },
    { id: 12, position: 2, type: "geoip", value: "ru", action: "direct", enabled: true, label: "" },
    { id: 13, position: 3, type: "geosite", value: "category-ads-all", action: "block", enabled: true, label: "" },
    { id: 14, position: 4, type: "domain", value: "example.test", action: "direct", enabled: false, label: "" },
    { id: 15, position: 5, type: "domain", value: "gosuslugi.ru", action: "direct", enabled: true, label: "" },
    { id: 16, position: 6, type: "ip", value: "10.0.0.0/8", action: "direct", enabled: true, label: "" },
  ],
};

/**
 * The ruleset Tunnel › Routing edits: one rule of every type, labels on some, the last one switched off, and an ip rule
 * on a public range (a private one with an action other than direct is refused by the backend).
 */
export const TUNNEL_ROUTING: Routing = {
  default_action: "proxy",
  domain_strategy: "IPIfNonMatch",
  rules: [
    { id: 21, position: 0, type: "geosite", value: "category-ads-all", action: "block", enabled: true, label: "ads" },
    { id: 22, position: 1, type: "geoip", value: "ru", action: "direct", enabled: true, label: "RU off tunnel" },
    { id: 23, position: 2, type: "domain", value: "*.ya.ru, yandex.net", action: "direct", enabled: true, label: "" },
    { id: 24, position: 3, type: "ip", value: "45.83.0.0/16", action: "proxy", enabled: true, label: "office" },
    { id: 25, position: 4, type: "port", value: "25", action: "block", enabled: true, label: "no SMTP" },
    { id: 26, position: 5, type: "domain", value: "netflix.com", action: "proxy", enabled: false, label: "" },
  ],
};

/** GET /routing/presets, as the backend lists them. */
export const ROUTING_PRESETS: PresetInfo[] = [
  { name: "ru-direct", title: "RU-direct — keep Russian traffic off the tunnel" },
  { name: "block-ads", title: "Block ads & trackers" },
  { name: "cn-direct", title: "CN-direct — Chinese traffic off the tunnel" },
  { name: "lan-direct", title: "LAN-direct — private ranges direct (explicit)" },
];

/**
 * POST /routing/preset/ru-direct over TUNNEL_ROUTING: the stored rules, then the preset's rules the store does not have
 * yet (geoip ru direct is already there) — unsaved, so with id 0.
 */
export const RU_DIRECT_PRESET: Routing = {
  ...TUNNEL_ROUTING,
  rules: [
    ...TUNNEL_ROUTING.rules,
    { id: 0, position: 6, type: "geosite", value: "category-ru", action: "direct", enabled: true, label: "" },
  ],
};

/** Validate replies: a pass, and the backend's refusal of a private range sent anywhere but direct. */
export const VALID = { ok: true, error: "" };
export const ROUTING_INVALID = {
  ok: false,
  error: "rule 4: '10.0.0.0/8' is a private range and the built-in 'geoip:private → direct' rule is matched first, so this rule could never send it to 'proxy' — only 'direct' is reachable for private ranges",
};
export const PROFILE_INVALID = { ok: false, error: "bad fragment length '0-5'" };

function profile(patch: Partial<TuningProfile> & Pick<TuningProfile, "id" | "name">): TuningProfile {
  return {
    fingerprint: "chrome", frag_enabled: false, frag_packets: "tlshello", frag_length: "100-200", frag_interval: "10-20",
    mux_enabled: false, mux_concurrency: "", xudp_proxy_udp443: "", doh_enabled: true, doh_url: "", quic: "allow",
    noise_enabled: false, noises: [], xhttp_padding: "", xmux_max_concurrency: "", xmux_max_connections: "",
    alpn: "", tls_min: "", tls_max: "", is_default: false, is_active: false, node_count: 0, ...patch,
  };
}

/**
 * Tunnel › Anti-DPI's profiles, every field set somewhere: balanced is the default (contract defaults); fragment-tls
 * governs the live tunnel, with fragmentation, two noise rows and QUIC dropped; mux-heavy is unused, with mux, the
 * XHTTP and TLS knobs, a DoH URL and QUIC proxied.
 */
export const TUNNEL_PROFILES: TuningProfile[] = [
  profile({ id: 1, name: "balanced", is_default: true, node_count: 8 }),
  profile({
    id: 2, name: "fragment-tls", is_active: true, node_count: 3, frag_enabled: true, quic: "drop", noise_enabled: true,
    noises: [{ type: "rand", packet: "50-150", delay: "10-16" }, { type: "hex", packet: "0a0b0c0d", delay: "20-40" }],
  }),
  profile({
    id: 3, name: "mux-heavy", fingerprint: "firefox", mux_enabled: true, mux_concurrency: "8", xudp_proxy_udp443: "skip",
    doh_enabled: false, doh_url: "https://1.1.1.1/dns-query", quic: "proxy", xhttp_padding: "100-1000",
    xmux_max_concurrency: "16", xmux_max_connections: "0", alpn: "h2,http/1.1", tls_min: "1.2", tls_max: "1.3",
  }),
];

/** GET /profiles/presets, as the backend lists them. */
export const PROFILE_PRESETS: ProfilePreset[] = [
  {
    name: "ru-hardened", title: "RU-hardened — fragment + noise + QUIC drop",
    fields: {
      fingerprint: "chrome", frag_enabled: true, frag_packets: "tlshello", frag_length: "100-200", frag_interval: "10-20", quic: "drop",
      noise_enabled: true, noises: [{ type: "rand", packet: "50-150", delay: "10-16" }],
    },
  },
  { name: "stealth-latency", title: "Stealth (min latency) — fingerprint only, QUIC allowed", fields: { fingerprint: "chrome", frag_enabled: false, quic: "allow" } },
  {
    name: "cdn-xhttp", title: "CDN / XHTTP — padding + xmux",
    fields: { fingerprint: "chrome", quic: "drop", xhttp_padding: "100-1000", xmux_max_concurrency: "16", xmux_max_connections: "0" },
  },
];

/** Status as the Health & failover strip reads it after a switch twelve minutes ago. */
export const FAILOVER_STATUS: Status = { ...STATUS, last_failover_at: NOW_SEC - 720, failovers_24h: 1, eligible_standby_count: 4 };

/** One live frame: healthy tunnel through nl-ams-03, nothing leaking around it. */
export const TRAFFIC_FRAME: TrafficFrame = {
  ts: NOW_SEC * 1000,
  outbounds: { proxy: { up_bps: 1_800_000, down_bps: 12_400_000 }, direct: { up_bps: 0, down_bps: 0 } },
  totals: { up: 1_300_000_000, down: 19_000_000_000 },
  lifetime: { up: 9_000_000_000, down: 120_000_000_000 },
  session: { up: 1_240_000_000, down: 18_600_000_000 },
  active: {
    node_id: 1, real_ok: true, stale: false, latency_ms: 42, egress_ip: "185.107.56.21", egress_ip6: "2a0b:4d07::21",
    egress_cc: "NL", egress_cc6: "NL", checked_at: iso(NOW_SEC - 5), lat_history: [40, 44, 39, 47, 52, 41, 90, 45, 42],
  },
};

/**
 * Enough of the backend for every screen built so far to render real content; tests override single
 * methods as needed. Each call also starts the test on the gateway clock with an empty live-traffic
 * store, so a frame from one test never shows up in the next.
 */
export function mockApi() {
  trafficStore.reset();
  recordServerNow(STATUS.server_now);
  let deliver: ((message: TrafficMessage) => void) | null = null;
  return {
    getStatus: vi.spyOn(api, "getStatus").mockResolvedValue(STATUS),
    listNodes: vi.spyOn(api, "listNodes").mockResolvedValue(NODES),
    getNetwork: vi.spyOn(api, "getNetwork").mockResolvedValue(NETWORK),
    listNodeHealth: vi.spyOn(api, "listNodeHealth").mockResolvedValue(NODE_HEALTH),
    listSubs: vi.spyOn(api, "listSubs").mockResolvedValue(SUBS),
    getRouting: vi.spyOn(api, "getRouting").mockResolvedValue(ROUTING),
    xrayStart: vi.spyOn(api, "xrayStart").mockResolvedValue({ ok: true }),
    xrayStop: vi.spyOn(api, "xrayStop").mockResolvedValue({ ok: true }),
    connectTraffic: vi.spyOn(api, "connectTraffic").mockImplementation((onMessage) => {
      deliver = onMessage;
      return { close: vi.fn(), resume: vi.fn() };
    }),
    getTrafficHistory: vi.spyOn(api, "getTrafficHistory").mockResolvedValue({ samples: [], interval_ms: 1000 }),
    listProfiles: vi.spyOn(api, "listProfiles").mockResolvedValue(PROFILES),
    getSettings: vi.spyOn(api, "getSettings").mockResolvedValue(SETTINGS),
    putSettings: vi.spyOn(api, "putSettings").mockImplementation(async (patch: Partial<Settings>) => ({ ...SETTINGS, ...patch })),
    // Writes answer like the gateway, so a screen test never reaches the network by accident.
    apply: vi.spyOn(api, "apply").mockResolvedValue({ ok: true }),
    disconnect: vi.spyOn(api, "disconnect").mockResolvedValue({ ok: true }),
    connectBest: vi.spyOn(api, "connectBest").mockResolvedValue({ ok: true, node_id: 2 }),
    probeNode: vi.spyOn(api, "probeNode").mockImplementation(async (id: number) => ALL_NODE_HEALTH.find((h) => h.node_id === id) ?? health(id, { checked_at: iso(NOW_SEC) })),
    probeTcp: vi.spyOn(api, "probeTcp").mockResolvedValue(NODE_HEALTH),
    probeHttp: vi.spyOn(api, "probeHttp").mockResolvedValue(NODE_HEALTH),
    reorderNodes: vi.spyOn(api, "reorderNodes").mockResolvedValue({ ok: true }),
    addNode: vi.spyOn(api, "addNode").mockImplementation(async (input: NodeIn) => ({ ...node(11, input.name), ...input })),
    updateNode: vi.spyOn(api, "updateNode").mockImplementation(async (id: number, patch: NodeUpdate) => ({ ...(ALL_NODES.find((n) => n.id === id) ?? node(id, `node-${id}`)), ...patch })),
    deleteNode: vi.spyOn(api, "deleteNode").mockResolvedValue({ ok: true }),
    importNodes: vi.spyOn(api, "importNodes").mockResolvedValue({ added: 2, total: 3, format: "clash" }),
    detachNodes: vi.spyOn(api, "detachNodes").mockResolvedValue({ ok: true }),
    validateNode: vi.spyOn(api, "validateNode").mockResolvedValue({ ok: true, error: "" }),
    addSub: vi.spyOn(api, "addSub").mockImplementation(async (input: SubscriptionIn) => sub({ id: 4, ...input, node_count: 0 })),
    updateSub: vi.spyOn(api, "updateSub").mockImplementation(async (id: number, patch: Partial<SubscriptionIn>) => ({ ...(SUBS.find((s) => s.id === id) ?? sub({ id, name: `sub-${id}` })), ...patch })),
    deleteSub: vi.spyOn(api, "deleteSub").mockResolvedValue({ ok: true }),
    refreshSub: vi.spyOn(api, "refreshSub").mockResolvedValue({ ok: true, status: "ok: +0 ~6 -0", error: null, path: "tunnel" }),
    refreshAllSubs: vi.spyOn(api, "refreshAllSubs").mockResolvedValue(REFRESH_ALL),
    previewSub: vi.spyOn(api, "previewSub").mockResolvedValue(PREVIEW),
    previewSubNodes: vi.spyOn(api, "previewSubNodes").mockResolvedValue(PREVIEW_NODES),
    listRoutingPresets: vi.spyOn(api, "listRoutingPresets").mockResolvedValue(ROUTING_PRESETS),
    routingPreset: vi.spyOn(api, "routingPreset").mockResolvedValue(RU_DIRECT_PRESET),
    validateRouting: vi.spyOn(api, "validateRouting").mockResolvedValue(VALID),
    putRouting: vi.spyOn(api, "putRouting").mockImplementation(async (body: RoutingIn) => ({
      rules: body.rules.map((rule, index) => ({ id: 100 + index, position: index, enabled: true, label: "", ...rule })),
      default_action: body.default_action,
      domain_strategy: body.domain_strategy ?? "IPIfNonMatch",
    })),
    listProfilePresets: vi.spyOn(api, "listProfilePresets").mockResolvedValue(PROFILE_PRESETS),
    validateProfile: vi.spyOn(api, "validateProfile").mockResolvedValue(VALID),
    addProfile: vi.spyOn(api, "addProfile").mockImplementation(async (input: ProfileIn) => profile({ ...input, id: 4 })),
    updateProfile: vi.spyOn(api, "updateProfile").mockImplementation(async (id: number, patch: ProfileUpdate) => ({
      ...(TUNNEL_PROFILES.find((p) => p.id === id) ?? profile({ id, name: `profile-${id}` })), ...patch,
    })),
    deleteProfile: vi.spyOn(api, "deleteProfile").mockResolvedValue({ ok: true }),
    setDefaultProfile: vi.spyOn(api, "setDefaultProfile").mockImplementation(async (id: number) => ({
      ...(TUNNEL_PROFILES.find((p) => p.id === id) ?? profile({ id, name: `profile-${id}` })), is_default: true,
    })),
    applyProfileActive: vi.spyOn(api, "applyProfileActive").mockResolvedValue({ ok: true, node_id: 1 }),
    /** Push one WebSocket message into the live-traffic store, as the socket would. */
    emitTraffic(message: TrafficMessage): void {
      const send = deliver;
      if (!send) throw new Error("no traffic subscriber yet: render a screen that shows live traffic first");
      act(() => send(message));
    },
  };
}

export type MockApi = ReturnType<typeof mockApi>;

/** Serve every group's nodes and their health instead of just "work" (see ALL_NODES). */
export function mockNodeGroups(api$: MockApi): MockApi {
  api$.listNodes.mockResolvedValue(ALL_NODES);
  api$.listNodeHealth.mockResolvedValue(ALL_NODE_HEALTH);
  return api$;
}

/** Serve Tunnel's ruleset and profiles instead of the Home ones (see TUNNEL_ROUTING, TUNNEL_PROFILES). */
export function mockTunnel(api$: MockApi): MockApi {
  api$.getRouting.mockResolvedValue(TUNNEL_ROUTING);
  api$.listProfiles.mockResolvedValue(TUNNEL_PROFILES);
  return api$;
}

/** Start a write under `mutationKey` that runs until the returned function is called, as one started elsewhere would. */
export function holdWrite(client: QueryClient, mutationKey: QueryKey): () => Promise<void> {
  let release: () => void = () => {};
  const done = new Promise<void>((resolve) => { release = resolve; });
  const running = client.getMutationCache().build(client, { mutationKey, mutationFn: () => done }).execute(undefined);
  return () => act(async () => { release(); await running; });
}

/**
 * Start a connection write (CONNECTION_WRITE) that runs until the returned function is called: every connection
 * control waits meanwhile, as it would for a connect started elsewhere in the app.
 */
export function holdConnectionWrite(client: QueryClient): () => Promise<void> {
  return holdWrite(client, CONNECTION_WRITE);
}
