import type { Network } from "./api";

// Pure presentation logic for the Network screen's live-status panel, so it's
// unit-testable from a sample payload without a DOM harness. Real detection is
// Pi-only; dev/unknown values render as "unknown" tone + em-dash.
export type Tone = "ok" | "bad" | "unknown";

export interface NetworkView {
  segment: { tone: Tone; label: string };
  dhcp_clients: number;
  tunnel: { tone: Tone; egress: string; latency: string };
}

function boolTone(v: boolean | null): Tone {
  return v === null ? "unknown" : v ? "ok" : "bad";
}

export function networkView(net: Network): NetworkView {
  const up = net.status.segment_up;
  const t = net.status.tunnel;
  return {
    segment: { tone: boolTone(up), label: up === null ? "unknown" : up ? "up" : "down" },
    dhcp_clients: net.status.dhcp_clients,
    tunnel: {
      tone: boolTone(t.real_ok),
      egress: t.egress_ip ?? "—",
      latency: t.latency_ms === null ? "—" : `${t.latency_ms} ms`,
    },
  };
}
