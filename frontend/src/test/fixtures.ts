import type { QueryClient, QueryKey } from "@tanstack/react-query";
import { act } from "@testing-library/react";
import { vi, type MockedFunction } from "vitest";
import type {
  ApiToken, ApiTokenCreated, ApiTokenScope, AuditEntry, BackupDoc, Diagnostics, Geo, Network, NetworkPatch, Node, NodeHealth, NodeIn, NodeUpdate,
  PresetInfo, PreviewNodes, Preview, ProfileIn, ProfilePreset, ProfileUpdate,
  RefreshAllResult, RestoreResult, Routing, RoutingIn, Rw, RwClient, RwIn, Settings, Status, Subscription, SubscriptionIn, TrafficFrame, TrafficMessage,
  TuningProfile,
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
    enforcement_status: "ok", enforcement_error: "",
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
    last_error: null, up_bytes: null, down_bytes: null, total_bytes: null, expire_at: null, node_count: 3,
    last_skipped: {}, ...patch,
  };
}

/**
 * "work" expires in two days, refreshes hourly through the tunnel and carries entries this panel cannot use; "home" is at 86 % of its allowance and its last
 * fetch failed; "old" is paused (never warns), expired, never refreshes on its own and has no nodes left.
 */
export const SUBS: Subscription[] = [
  sub({
    id: 1, name: "work", url: "https://sub.work-vpn.example/api/v1/client/subscribe?token=9f2c7a1e", interval_sec: 3_600,
    injection: { headers: { "x-device-os": "{device_os}", "user-agent": "v2pi/1.0" }, query: { type: "vless" } },
    expire_at: NOW_SEC + 2 * 86_400, up_bytes: 2e9, down_bytes: 18e9, total_bytes: 100e9,
    last_status: "ok: +0 ~6 -0 (5 unsupported)", last_path: "tunnel", last_fetched: iso(NOW_SEC - 240), node_count: 6,
    last_skipped: { trojan: 3, hysteria2: 1, invalid: 1 },
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
  dns_intercept: false, session_timeout_min: 60, auto_backup_enabled: true, update_check_enabled: true,
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
  format: "base64/vless", count: 214, returned_count: 200, truncated: true, skipped: { trojan: 4, hysteria2: 2 },
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
    { id: 11, position: 1, type: "domain", value: "netflix.com", action: "proxy", enabled: true, label: "", dataset: "" },
    { id: 12, position: 2, type: "geoip", value: "ru", action: "direct", enabled: true, label: "", dataset: "" },
    { id: 13, position: 3, type: "geosite", value: "category-ads-all", action: "block", enabled: true, label: "", dataset: "" },
    { id: 14, position: 4, type: "domain", value: "example.test", action: "direct", enabled: false, label: "", dataset: "" },
    { id: 15, position: 5, type: "domain", value: "gosuslugi.ru", action: "direct", enabled: true, label: "", dataset: "" },
    { id: 16, position: 6, type: "ip", value: "10.0.0.0/8", action: "direct", enabled: true, label: "", dataset: "" },
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
    { id: 21, position: 0, type: "geosite", value: "category-ads-all", action: "block", enabled: true, label: "ads", dataset: "" },
    { id: 22, position: 1, type: "geoip", value: "ru", action: "direct", enabled: true, label: "RU off tunnel", dataset: "" },
    { id: 23, position: 2, type: "domain", value: "*.ya.ru, yandex.net", action: "direct", enabled: true, label: "", dataset: "" },
    { id: 24, position: 3, type: "ip", value: "45.83.0.0/16", action: "proxy", enabled: true, label: "office", dataset: "" },
    { id: 25, position: 4, type: "port", value: "25", action: "block", enabled: true, label: "no SMTP", dataset: "" },
    { id: 26, position: 5, type: "domain", value: "netflix.com", action: "proxy", enabled: false, label: "", dataset: "" },
  ],
};

/** GET /routing/presets, as the backend lists them. */
export const ROUTING_PRESETS: PresetInfo[] = [
  { name: "ru-direct", title: "RU-direct — keep Russian traffic off the tunnel", dataset: "", default_action: null },
  { name: "block-ads", title: "Block ads & trackers", dataset: "", default_action: null },
  { name: "cn-direct", title: "CN-direct — Chinese traffic off the tunnel", dataset: "", default_action: null },
  { name: "lan-direct", title: "LAN-direct — private ranges direct (explicit)", dataset: "", default_action: null },
  { name: "ru-blocked-only", title: "Only blocked-in-RU through the tunnel (needs the RU geo data)", dataset: "ru", default_action: "direct" },
];

/**
 * POST /routing/preset/ru-direct over TUNNEL_ROUTING: the stored rules, then the preset's rules the store does not have
 * yet (geoip ru direct is already there) — unsaved, so with id 0.
 */
export const RU_DIRECT_PRESET: Routing = {
  ...TUNNEL_ROUTING,
  rules: [
    ...TUNNEL_ROUTING.rules,
    { id: 0, position: 6, type: "geosite", value: "category-ru", action: "direct", enabled: true, label: "", dataset: "" },
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

/**
 * Gateway › Network's read: the segment on eth0.2 at 192.168.50.1 with DHCP .100–.200 handing out the gateway as DNS,
 * IPv6 through the tunnel on a static /64, the kill-switch armed and confirmed, seven leases in no particular order (one
 * without a host name, one that never expires) and the router checklist the backend builds for that saved plan.
 */
export const GATEWAY_NETWORK: Network = {
  segment: {
    iface: "eth0.2", ip: "192.168.50.1", ip6: "2001:db8:5a:2::/64", dhcp_start: "192.168.50.100", dhcp_end: "192.168.50.200",
    dhcp_lease: "12h", client_dns: "192.168.50.1", client_dns6: "2606:4700:4700::1111",
  },
  kill_switch_enabled: true, lan_access_enabled: true, ipv6_enabled: true,
  status: {
    segment_up: true, uplink: true, uplink6: true, dhcp_clients: 7,
    clients: [
      { ip: "192.168.50.140", mac: "aa:bb:cc:00:01:40", hostname: "thinkpad-work", expiry: NOW_SEC + 3 * 3_600 + 120 },
      { ip: "192.168.50.101", mac: "aa:bb:cc:00:01:01", hostname: "iphone-anna", expiry: NOW_SEC + 11 * 3_600 + 900 },
      { ip: "192.168.50.176", mac: "aa:bb:cc:00:01:76", hostname: "ipad", expiry: NOW_SEC + 38 * 60 + 20 },
      { ip: "192.168.50.112", mac: "aa:bb:cc:00:01:12", hostname: "", expiry: NOW_SEC + 47 * 60 + 30 },
      { ip: "192.168.50.123", mac: "aa:bb:cc:00:01:23", hostname: "appletv", expiry: 0 },
      { ip: "192.168.50.104", mac: "aa:bb:cc:00:01:04", hostname: "macbook-pro", expiry: NOW_SEC + 9 * 3_600 + 60 },
      { ip: "192.168.50.118", mac: "aa:bb:cc:00:01:18", hostname: "pixel-8", expiry: NOW_SEC + 6 * 3_600 + 1_800 },
    ],
    tunnel: { real_ok: true, latency_ms: 42, egress_ip: "185.107.56.21", checked_at: iso(NOW_SEC - 5) },
    wan_blocked: false, enforcement_status: "ok", enforcement_error: "", enforcement_warning: "",
    ipv6_prefix: null, foreign_ra: false, ipv6_prefix_source: "static",
  },
  recommendations: [
    { title: "Create VLAN 2", detail: "Add VLAN 2 on the router and tag the client switch port to it (the Pi's client leg is eth0.2)." },
    { title: "Disable the router's DHCP on VLAN 2", detail: "The Pi serves DHCP + DNS on this segment (192.168.50.100–192.168.50.200); two DHCP servers on one VLAN conflict." },
    { title: "Give the Pi's Home leg internet", detail: "The Pi reaches the tunnel through its Home leg eth0 (192.168.1.120); keep that port on your normal LAN with internet access." },
    { title: "Delegate an IPv6 /64 to VLAN 2", detail: "Route a v6 /64 to this segment — DHCPv6-PD on the router, or a static route of 2001:db8:5a:2::/64 to the Pi's Home leg eth0. (Set the prefix to `auto` to read it from a host PD client instead.)" },
    { title: "Disable the router's IPv6 / Router Advertisement on VLAN 2", detail: "The Pi advertises IPv6 (RA) on this segment itself; a second router advertising its ISP prefix here makes clients leak around the tunnel (they'd pick the router's prefix, not the Pi's)." },
    { title: "Use a node with IPv6 egress", detail: "v6 traffic exits via the active node; pick one with working IPv6 or v6-only sites will fail (no leak — they just won't connect)." },
  ],
  events: [],
};

const SEGMENT_FIELDS = {
  segment_iface: "iface", segment_ip: "ip", segment_ip6: "ip6", dhcp_start: "dhcp_start", dhcp_end: "dhcp_end", dhcp_lease: "dhcp_lease",
  client_dns: "client_dns", client_dns6: "client_dns6",
} as const;

/** What PUT /network answers: the stored settings with the patch's values, as the backend reads them back. */
export function networkAfter(patch: NetworkPatch, base: Network = GATEWAY_NETWORK): Network {
  const segment = { ...base.segment };
  for (const key of Object.keys(SEGMENT_FIELDS) as (keyof typeof SEGMENT_FIELDS)[]) {
    const value = patch[key];
    if (value !== undefined) segment[SEGMENT_FIELDS[key]] = value;
  }
  return {
    ...base, segment,
    kill_switch_enabled: patch.kill_switch_enabled ?? base.kill_switch_enabled,
    lan_access_enabled: patch.lan_access_enabled ?? base.lan_access_enabled,
    ipv6_enabled: patch.ipv6_enabled ?? base.ipv6_enabled,
  };
}

/**
 * 32 sequential bytes starting at `first`, as unpadded base64url: 43 characters, the exact shape of an x25519 key or a
 * token secret, and fake on sight. Built rather than pasted, as backend/tests/test_xray_real.py does, so no fixture
 * carries a literal a reader or the secret scan could mistake for a real one.
 */
function byteRamp(first: number): string {
  const bytes = Array.from({ length: 32 }, (_, i) => first + i);
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/**
 * Test vectors shaped like `xray x25519` output — 43 base64url characters of 32 bytes, the byte ramps 0x20–0x3F and
 * 0x00–0x1F the backend's tests use. Fake on inspection; never real keys.
 */
export const RW_PUBLIC_KEY = byteRamp(0x20);
export const RW_PRIVATE_KEY = byteRamp(0x00);

/** The three devices: iphone-anna and laptop-work active, ipad suspended. */
export const RW_CLIENTS: RwClient[] = [
  { id: "3f2a9c1e-7b4d-4e8a-9c21-5d6e7f809a1b", email: "iphone-anna", enabled: true },
  { id: "b01c55d2-9e3f-4a6b-8c7d-1e2f3a4b5c6d", email: "ipad", enabled: false },
  { id: "e04d77aa-1b2c-4d3e-9f40-a1b2c3d4e5f6", email: "laptop-work", enabled: true },
];

/**
 * Gateway › Remote access's read: the inbound on :8443 with a stored private key and served live, two LAN hosts by
 * name, the routed subnets derived from the management and segment /24s, and the three devices of RW_CLIENTS.
 */
export const RW: Rw = {
  enabled: true, port: 8443, dest: "www.microsoft.com:443", server_names: "www.microsoft.com,learn.microsoft.com", short_ids: "3a9e,6ba85179e3d4fc21",
  public_key: RW_PUBLIC_KEY, endpoint: "vpn.example.net", has_private_key: true,
  hosts: { "nas.v2pi": "192.168.1.10", "printer.v2pi": "192.168.1.20" }, state_error: "",
  routed_nets: ["192.168.1.0/24", "192.168.50.0/24"], routed_nets_override: "",
  clients: RW_CLIENTS, live: true, revocation: "", revocation_pending: false,
};

/** The same gateway while a committed revocation has not been proven to reach the running xray. */
export const RW_PENDING: Rw = { ...RW, revocation_pending: true };

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

// --- System ---

/**
 * Three API tokens as `GET /tokens` orders them (by id ascending): one that has never been used and
 * expires inside a month, one used minutes ago that expires later, and one that never expires.
 */
export const TOKENS: ApiToken[] = [
  { id: 1, name: "grafana-monitor", scope: "monitor", prefix: "pgwp_8Kq2Xm4", created_at: NOW_SEC - 60 * 86_400, last_used_at: null, expires_at: NOW_SEC + 30 * 86_400 },
  { id: 2, name: "ci-deploy", scope: "readwrite", prefix: "pgwp_Vt9pLs1", created_at: NOW_SEC - 30 * 86_400, last_used_at: NOW_SEC - 120, expires_at: NOW_SEC + 90 * 86_400 },
  { id: 3, name: "uptime-probe", scope: "read", prefix: "pgwp_Zc3hRw7", created_at: NOW_SEC - 120 * 86_400, last_used_at: NOW_SEC - 2 * 86_400, expires_at: null },
];

/**
 * The secret `POST /tokens` hands out, shaped as tokens.generate() makes one — `pgwp_` and 32 bytes of base64url — but
 * the bytes are the ramp 0x40–0x5F, the next one after the remote-access keys, so it is fake on sight.
 */
const CREATED_TOKEN = `pgwp_${byteRamp(0x40)}`;

/** `POST /tokens`' reply: the row plus the secret, which exists only in this one response. */
export const TOKEN_CREATED: ApiTokenCreated = {
  id: 4, name: "home-assistant", scope: "monitor", prefix: CREATED_TOKEN.slice(0, 12), created_at: NOW_SEC, last_used_at: null, expires_at: NOW_SEC + 30 * 86_400,
  token: CREATED_TOKEN,
};

/**
 * Eight audit rows, newest first, covering every shape the list has to render: a remote-access client
 * path carrying a live uuid, a token actor, `anon`, and 2xx / 4xx / the middleware's own 413.
 */
export const AUDIT: AuditEntry[] = [
  { ts: NOW_SEC - 30, actor: "user:admin", method: "PATCH", path: "/api/rw/clients/3f2a1c4e-77b0-4d51-9a2e-8c1b6f0d4a75", status: 200 },
  { ts: NOW_SEC - 120, actor: "token:pgwp_Vt9pLs1", method: "PUT", path: "/api/settings", status: 200 },
  { ts: NOW_SEC - 300, actor: "user:admin", method: "POST", path: "/api/tokens", status: 201 },
  { ts: NOW_SEC - 900, actor: "user:admin", method: "DELETE", path: "/api/tokens/4", status: 204 },
  { ts: NOW_SEC - 1_800, actor: "user:admin", method: "POST", path: "/api/password", status: 403 },
  { ts: NOW_SEC - 2_400, actor: "anon", method: "POST", path: "/api/login", status: 401 },
  { ts: NOW_SEC - 3_000, actor: "user:admin", method: "POST", path: "/api/settings/reset", status: 200 },
  { ts: NOW_SEC - 4_200, actor: "anon", method: "POST", path: "/api/restore", status: 413 },
];

/** Seven `data/app.log` lines: two ERROR, one WARNING, and four INFO from three loggers. */
export const LOG_LINES: string[] = [
  "2023-11-14 22:10:58,003 INFO pi_gw_panel.xray_supervisor.supervisor starting xray (pid 2417)",
  "2023-11-14 22:11:02,447 INFO pi_gw_panel.controller applied node 12 — xray reloaded in 1.4s",
  "2023-11-14 22:12:44,118 ERROR pi_gw_panel.xray_supervisor.supervisor refusing to start xray: the config on disk may not be served",
  "2023-11-14 22:12:47,902 ERROR pi_gw_panel xray (pid 2417) is still running 5s after SIGKILL; it was NOT stopped",
  "2023-11-14 22:13:05,660 WARNING pi_gw_panel.xray_config.validate rolled back to the last good xray config",
  "2023-11-14 22:13:09,214 INFO pi_gw_panel.xray_supervisor.supervisor xray running again (pid 2604)",
  "2023-11-14 22:38:12,417 INFO pi_gw_panel.api.routes stats client reconfigured to 127.0.0.1:10085 — xray StatsService reachable",
];

/**
 * The geo data as a gateway that has installed the RU lists holds it: the stock files are the
 * image's (old), the RU ones were updated today and can still be reverted.
 */
export const GEO: Geo = {
  asset_dir: "/app/data/geo",
  disk_free_bytes: 11_400_000_000,
  files: [
    { dataset: "stock", file: "geoip.dat", source: "v2fly/geoip/geoip.dat", present: true, bytes: 19_768_301, updated_at: NOW_SEC - 90 * 86_400, has_previous: false },
    { dataset: "stock", file: "geosite.dat", source: "v2fly/domain-list-community/dlc.dat", present: true, bytes: 10_491_954, updated_at: NOW_SEC - 90 * 86_400, has_previous: false },
    { dataset: "ru", file: "geoip_ru.dat", source: "runetfreedom/russia-v2ray-rules-dat/geoip.dat", present: true, bytes: 18_355_076, updated_at: NOW_SEC - 3_600, has_previous: true },
    { dataset: "ru", file: "geosite_ru.dat", source: "runetfreedom/russia-v2ray-rules-dat/geosite.dat", present: true, bytes: 73_703_302, updated_at: NOW_SEC - 3_600, has_previous: true },
  ],
};

/** A healthy gateway's diagnostics: the collector last succeeded two seconds ago and has never failed. */
export const DIAGNOSTICS: Diagnostics = {
  app_version: "1.18.64", xray_version: "25.3.6", uptime_sec: 6 * 86_400 + 4 * 3_600 + 12 * 60,
  db_path: "/app/data/pi_gw_panel.sqlite", db_bytes: 2_100_000,
  disk_free_bytes: 11_400_000_000, disk_total_bytes: 29_000_000_000,
  stats_last_ok_at: NOW_SEC - 2, stats_error: "", stats_fail_count: 0,
  // Checked an hour ago: the panel is a release behind, xray is current (B2).
  latest_app_version: "v1.19", latest_xray_version: "v25.3.6",
  app_update_available: true, xray_update_available: false,
  update_checked_at: NOW_SEC - 3_600, update_error: "", update_check_enabled: true,
};

/**
 * What `GET /backup` hands back: the whole configuration, and no `created_at` — only the daily job stamps one.
 * Nodes, subscriptions and profiles each carry `id`, as the real export always does — `BackupNode`, `BackupSubscription`
 * and `BackupProfile` declare it `Field(gt=0)` with no default (backend/pi_gw_panel/backup/__init__.py:182,148,124),
 * and `export_state`'s `_node_dict`/`_profile_dict`/the subscription literal always include it (:405-441).
 */
export const BACKUP_DOC: BackupDoc = {
  schema_version: 2,
  nodes: [{ id: 1, name: "nl-ams-03", address: "nl-ams-03.example.org", port: 443, uuid: "uuid-1" }],
  subscriptions: [{ id: 1, name: "work", url: "https://example.org/sub" }],
  profiles: [{ id: 1, name: "fragment-tls" }],
  routing: { rules: [{ type: "geoip", value: "ru", action: "direct" }], default_action: "proxy" },
  settings: { stats_enabled: "1", traffic_sample_ms: "1000" },
};

/** `POST /restore`'s reply on success: counts, a gateway that is now disconnected, and the snapshot path. */
export const RESTORE_RESULT: RestoreResult = {
  ok: true,
  restored: { nodes: 24, subscriptions: 3, profiles: 6, routing_rules: 11, rw_disabled: "" },
  runtime: "disconnected",
  pre_restore_snapshot: "/app/data/backups/pre-restore-1700000000-9c4e1f2a7b6d4e8fa0c35d71e2b48f60.json",
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
    importNodes: vi.spyOn(api, "importNodes").mockResolvedValue({ added: 2, total: 3, format: "clash", skipped: {} }),
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
      rules: body.rules.map((rule, index) => ({ id: 100 + index, position: index, enabled: true, label: "", dataset: "", ...rule })),
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
    putNetwork: vi.spyOn(api, "putNetwork").mockImplementation(async (patch: NetworkPatch) => networkAfter(patch)),
    getRw: vi.spyOn(api, "getRw").mockResolvedValue(RW),
    putRw: vi.spyOn(api, "putRw").mockImplementation(async (body: RwIn) => ({
      ...RW, enabled: body.enabled, port: body.port, dest: body.dest, server_names: body.server_names, short_ids: body.short_ids,
      public_key: body.public_key, endpoint: body.endpoint, has_private_key: RW.has_private_key || body.private_key !== "",
      hosts: body.hosts, routed_nets_override: body.routed_nets,
    })),
    addRwClient: vi.spyOn(api, "addRwClient").mockImplementation(async (email: string) => ({
      ...RW, clients: [...RW.clients, { id: "5b1d0c7e-2f3a-4b5c-8d6e-7f8091a2b3c4", email, enabled: true }],
    })),
    setRwClientEnabled: vi.spyOn(api, "setRwClientEnabled").mockImplementation(async (id: string, enabled: boolean) => ({
      ...RW, clients: RW.clients.map((client) => (client.id === id ? { ...client, enabled } : client)), revocation: enabled ? "" : "reapplied",
    })),
    deleteRwClient: vi.spyOn(api, "deleteRwClient").mockImplementation(async (id: string) => ({
      ...RW, clients: RW.clients.filter((client) => client.id !== id), revocation: "reapplied",
    })),
    newRwShortId: vi.spyOn(api, "newRwShortId").mockResolvedValue({ short_id: "0123456789abcdef" }),
    rwClientLink: vi.spyOn(api, "rwClientLink").mockImplementation(async (id: string) => ({
      link: `vless://${id}@vpn.example.net:8443?security=reality&pbk=${RW_PUBLIC_KEY}&sid=3a9e#${RW.clients.find((client) => client.id === id)?.email ?? "client"}`,
    })),
    rwClientConfig: vi.spyOn(api, "rwClientConfig").mockImplementation(async (id: string) => {
      const name = RW.clients.find((client) => client.id === id)?.email ?? "client";
      return { filename: `${name}.conf`, config: `[General]\nbypass-system = true\n[Proxy]\n${name} = vless, vpn.example.net, 8443\n` };
    }),
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

/** Serve Gateway › Network's segment, leases and checklist instead of the Home network read (see GATEWAY_NETWORK). */
export function mockGateway(api$: MockApi): MockApi {
  api$.getNetwork.mockResolvedValue(GATEWAY_NETWORK);
  return api$;
}

/**
 * Answer every System read and write: the backup document and its restore, the token list and its two writes, the
 * audit log, the log tail, diagnostics, the password rotation and the settings reset. Kept out of `mockApi` because no screen outside System
 * reads any of them, and a test that forgets this mock should fail loudly rather than quietly polling a real fetch.
 */
export function mockSystem(api$: MockApi): MockApi & SystemMocks {
  const extra: SystemMocks = {
    getBackup: vi.spyOn(api, "getBackup").mockResolvedValue(BACKUP_DOC),
    restore: vi.spyOn(api, "restore").mockResolvedValue(RESTORE_RESULT),
    changePassword: vi.spyOn(api, "changePassword").mockResolvedValue({ ok: true }),
    listTokens: vi.spyOn(api, "listTokens").mockResolvedValue(TOKENS),
    createToken: vi.spyOn(api, "createToken").mockImplementation(async (name: string, scope: ApiTokenScope, expiresAt?: number) => ({
      ...TOKEN_CREATED, name, scope, expires_at: expiresAt ?? null,
    })),
    deleteToken: vi.spyOn(api, "deleteToken").mockResolvedValue(undefined),
    listAudit: vi.spyOn(api, "listAudit").mockResolvedValue(AUDIT),
    checkUpdates: vi.spyOn(api, "checkUpdates").mockResolvedValue(DIAGNOSTICS),
    getGeo: vi.spyOn(api, "getGeo").mockResolvedValue(GEO),
    updateGeo: vi.spyOn(api, "updateGeo").mockImplementation(async (dataset: string) => ({
      ok: true, dataset, files: GEO.files.filter((f) => f.dataset === dataset).map((f) => f.file),
      reloaded: true, error: "", geo: GEO,
    })),
    revertGeo: vi.spyOn(api, "revertGeo").mockImplementation(async (dataset: string) => ({
      ok: true, dataset, files: [], reloaded: true, error: "", geo: GEO,
    })),
    getLogs: vi.spyOn(api, "getLogs").mockImplementation(async (source: string) => ({
      // Only `app` has content, for two different reasons: xray-error/xray-access are empty because
      // nothing ever writes those files (xray is built with no error path and "access": "none"), while
      // xray-stderr — the supervisor's in-memory redacted tail — is empty here only because this fixture
      // models a healthy gateway with nothing to report.
      source, lines: source === "app" ? LOG_LINES : [],
    })),
    getDiagnostics: vi.spyOn(api, "getDiagnostics").mockResolvedValue(DIAGNOSTICS),
    resetSettings: vi.spyOn(api, "resetSettings").mockResolvedValue(SETTINGS),
  };
  return Object.assign(api$, extra);
}

interface SystemMocks {
  changePassword: MockedFunction<typeof api.changePassword>;
  getBackup: MockedFunction<typeof api.getBackup>;
  restore: MockedFunction<typeof api.restore>;
  listTokens: MockedFunction<typeof api.listTokens>;
  createToken: MockedFunction<typeof api.createToken>;
  deleteToken: MockedFunction<typeof api.deleteToken>;
  listAudit: MockedFunction<typeof api.listAudit>;
  getLogs: MockedFunction<typeof api.getLogs>;
  getDiagnostics: MockedFunction<typeof api.getDiagnostics>;
  getGeo: MockedFunction<typeof api.getGeo>;
  updateGeo: MockedFunction<typeof api.updateGeo>;
  revertGeo: MockedFunction<typeof api.revertGeo>;
  checkUpdates: MockedFunction<typeof api.checkUpdates>;
  resetSettings: MockedFunction<typeof api.resetSettings>;
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
