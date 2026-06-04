import type { Network } from "./api";

// Pure presentation logic for the Network screen's live-status panel, so it's
// unit-testable from a sample payload without a DOM harness. Real detection is
// Pi-only; dev/unknown values render as "unknown" tone + em-dash.
export type Tone = "ok" | "bad" | "unknown";

export interface NetworkView {
  segment: { tone: Tone; label: string };
  uplink: { tone: Tone; label: string };
  dhcp_clients: number;
  tunnel: { tone: Tone; egress: string; latency: string };
  wan_blocked: boolean;
}

function boolTone(v: boolean | null | undefined): Tone {
  return v === null || v === undefined ? "unknown" : v ? "ok" : "bad";
}

export function networkView(net: Network): NetworkView {
  const up = net.status.segment_up;
  const ul = net.status.uplink ?? null;
  const t = net.status.tunnel;
  return {
    segment: { tone: boolTone(up), label: up === null ? "unknown" : up ? "up" : "down" },
    uplink: { tone: boolTone(ul), label: ul === null ? "unknown" : ul ? "up" : "down" },
    dhcp_clients: net.status.dhcp_clients,
    tunnel: {
      tone: boolTone(t.real_ok),
      egress: t.egress_ip ?? "—",
      latency: t.latency_ms === null ? "—" : `${t.latency_ms} ms`,
    },
    wan_blocked: net.status.wan_blocked ?? false,
  };
}
