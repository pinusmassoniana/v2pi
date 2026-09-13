import type { Node, Status } from "../api/client";

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
