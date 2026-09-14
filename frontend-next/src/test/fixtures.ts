import type { Node, Status } from "../api/client";
import { vi } from "vitest";
import { api } from "../api/client";

export const STATUS: Status = {
  running: true, pid: 7, active_node_id: 1, xray_state: "working", active_since: 1_700_000_000,
  last_failover_at: null, prev_active_node_id: null, server_now: 1_700_000_100, tunnel_online: true,
};

export function node(id: number, name: string): Node {
  return {
    id, name, address: `${name}.example.org`, port: 443, uuid: `uuid-${id}`, transport: "vision",
    network: "tcp", security: "reality", sni: "", public_key: "", short_id: "", fingerprint: "chrome",
    path: "", host: "", mode: "", alpn: "", note: "", subscription_id: null, stale: false,
    tuning_profile_id: null,
  };
}

/** Enough of the backend for the shell to render; tests override single methods as needed. */
export function mockApi() {
  return {
    getStatus: vi.spyOn(api, "getStatus").mockResolvedValue(STATUS),
    listNodes: vi.spyOn(api, "listNodes").mockResolvedValue([node(1, "nl-ams-03"), node(2, "de-fra-01")]),
    xrayStart: vi.spyOn(api, "xrayStart").mockResolvedValue({ ok: true }),
    xrayStop: vi.spyOn(api, "xrayStop").mockResolvedValue({ ok: true }),
    connectTraffic: vi.spyOn(api, "connectTraffic").mockReturnValue({ close: vi.fn(), resume: vi.fn() }),
    getTrafficHistory: vi.spyOn(api, "getTrafficHistory").mockResolvedValue({ samples: [], interval_ms: 1000 }),
  };
}
