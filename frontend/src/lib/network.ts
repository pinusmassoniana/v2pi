import type { Network } from "../api/client";
import type { Tone as StateTone } from "../components/data/types";

/**
 * O4 / W6 kill-switch, from what the gateway has saved and confirmed — never from an unsaved toggle. Switched off is
 * OPEN; ARMED only when the host confirms enforcement; anything else is UNKNOWN. Home and Gateway › Network both read
 * this one rule, so the two screens can never disagree.
 */
export function killSwitchState(network: Network | undefined): { label: "ARMED" | "OPEN" | "UNKNOWN"; tone: StateTone } {
  if (network?.kill_switch_enabled === false) return { label: "OPEN", tone: "bad" };
  if (network?.status.enforcement_status === "ok") return { label: "ARMED", tone: "ok" };
  return { label: "UNKNOWN", tone: "neutral" };
}

/** O10: static / ula / pd, "on" when enabled without a reported source, "off". */
export function ipv6Source(network: Network): string {
  if (!network.ipv6_enabled) return "off";
  const source = network.status.ipv6_prefix_source;
  return source === "static" || source === "ula" || source === "pd" ? source : "on";
}

// Pure presentation logic for the Network screen's live-status panel, so it's
// unit-testable from a sample payload without a DOM harness. Real detection is
// Pi-only; dev/unknown values render as "unknown" tone + em-dash.
export type Tone = "ok" | "bad" | "unknown";

export interface NetworkView {
  segment: { tone: Tone; label: string };
  uplink: { tone: Tone; label: string };
  uplink6: { tone: Tone; label: string };
  dhcp_clients: number;
  tunnel: { tone: Tone; egress: string; latency: string };
  wan_blocked: boolean;
  foreign_ra: boolean;
}

function boolTone(v: boolean | null | undefined): Tone {
  return v === null || v === undefined ? "unknown" : v ? "ok" : "bad";
}

export function networkView(net: Network): NetworkView {
  const up = net.status.segment_up;
  const ul = net.status.uplink ?? null;
  const ul6 = net.status.uplink6 ?? null;
  const t = net.status.tunnel ?? null;
  return {
    segment: { tone: boolTone(up), label: up === null ? "unknown" : up ? "up" : "down" },
    uplink: { tone: boolTone(ul), label: ul === null ? "unknown" : ul ? "up" : "down" },
    uplink6: { tone: boolTone(ul6), label: ul6 === null ? "unknown" : ul6 ? "up" : "down" },
    dhcp_clients: net.status.dhcp_clients,
    tunnel: {
      tone: boolTone(t?.real_ok),
      egress: t?.egress_ip ?? "—",
      latency: t?.latency_ms != null ? `${t.latency_ms} ms` : "—",
    },
    wan_blocked: net.status.wan_blocked ?? false,
    foreign_ra: net.status.foreign_ra === true,
  };
}
