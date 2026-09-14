import type { QueryClient } from "@tanstack/react-query";
import { act } from "@testing-library/react";
import { vi } from "vitest";
import type { Network, Node, NodeHealth, Routing, Status, Subscription, TrafficFrame, TrafficMessage } from "../api/client";
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

/** Six servers: the active one, three probed standbys (one slow and old), one failed, one never probed. */
export const NODES: Node[] = [
  node(1, "nl-ams-03"), node(2, "de-fra-01"), node(3, "fi-hel-02"),
  node(4, "pl-waw-01"), node(5, "ch-zrh-02"), node(6, "se-sto-01"),
];

function health(nodeId: number, patch: Partial<NodeHealth>): NodeHealth {
  return {
    node_id: nodeId, last_tcp_ok: true, last_tcp_ms: null, last_http_ok: true, last_http_ms: null,
    last_real_ok: true, last_real_ms: null, egress_ip: null, egress_ip6: null, egress_cc: null, egress_cc6: null,
    checked_at: null, fail_count: 0, lat_history: [], ...patch,
  };
}

/** Background-sweep results. ch-zrh-02 (id 5) has never been probed. */
export const NODE_HEALTH: NodeHealth[] = [
  health(1, { last_tcp_ms: 31, last_real_ms: 45, egress_ip: "185.107.56.21", egress_cc: "NL", checked_at: iso(NOW_SEC - 5), lat_history: [44, 45] }),
  health(2, { last_tcp_ms: 40, last_real_ms: 58, egress_ip: "91.198.10.4", egress_cc: "DE", checked_at: iso(NOW_SEC - 240) }),
  health(3, { last_tcp_ms: 52, last_real_ms: 71, egress_ip: "95.216.3.9", egress_cc: "FI", checked_at: iso(NOW_SEC - 360) }),
  health(4, { last_tcp_ms: 120, last_real_ms: 164, egress_ip: "51.68.22.7", egress_cc: "PL", checked_at: iso(NOW_SEC - 1_380) }),
  health(6, { last_tcp_ok: true, last_tcp_ms: 80, last_real_ok: false, last_real_ms: null, egress_cc: "SE", checked_at: iso(NOW_SEC - 720), fail_count: 3 }),
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

/** "work" expires in two days, "home" is at 86 % of its allowance, "old" is disabled (never warns). */
export const SUBS: Subscription[] = [
  sub({ id: 1, name: "work", expire_at: NOW_SEC + 2 * 86_400, up_bytes: 2e9, down_bytes: 18e9, total_bytes: 100e9 }),
  sub({ id: 2, name: "home", up_bytes: 6e9, down_bytes: 80e9, total_bytes: 100e9 }),
  sub({ id: 3, name: "old", enabled: false, expire_at: NOW_SEC - 86_400 }),
];

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
    /** Push one WebSocket message into the live-traffic store, as the socket would. */
    emitTraffic(message: TrafficMessage): void {
      const send = deliver;
      if (!send) throw new Error("no traffic subscriber yet: render a screen that shows live traffic first");
      act(() => send(message));
    },
  };
}

/**
 * Start a connection write (CONNECTION_WRITE) that runs until the returned function is called: every connection
 * control waits meanwhile, as it would for a connect started elsewhere in the app.
 */
export function holdConnectionWrite(client: QueryClient): () => Promise<void> {
  let release: () => void = () => {};
  const done = new Promise<void>((resolve) => { release = resolve; });
  const running = client.getMutationCache().build(client, { mutationKey: CONNECTION_WRITE, mutationFn: () => done }).execute(undefined);
  return () => act(async () => { release(); await running; });
}
