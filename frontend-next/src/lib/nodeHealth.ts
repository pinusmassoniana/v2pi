// Node-health rules shared by Home and Nodes: which node is active, what its live probe says, and how a
// probe result reads. Pure and unit-tested; no rendering here.
import type { Node, NodeHealth, Status, TrafficFrame } from "../api/client";
import type { LatencyRowData } from "../components/data/types";
import { flagEmoji } from "./flag";

/** The live probe a traffic frame carries (about one node), or null. */
export type ActiveProbe = TrafficFrame["active"];

/** A standby probe older than this is dimmed. */
export const PROBE_DIM_MS = 10 * 60_000;
/** Standby rows Home › Overview shows. */
export const STANDBY_ROWS = 4;
/** A latency above this reads as slow (amber) everywhere: latency bars and node pills. */
export const SLOW_LATENCY_MS = 150;

export function activeNode(nodes: readonly Node[] | undefined, activeId: number | null | undefined): Node | undefined {
  return activeId === null || activeId === undefined ? undefined : nodes?.find((n) => n.id === activeId);
}

/** A node's name; "node #N" while the node list has not loaded (or no longer lists it). */
export function nodeLabel(nodes: readonly Node[] | undefined, id: number): string {
  return nodes?.find((n) => n.id === id)?.name ?? `node #${id}`;
}

/** The active node as every surface names it: its name, "node #N" before the list loads, "No node" when none is active. */
export function activeNodeLabel(status: Status | undefined, nodes: readonly Node[] | undefined): string {
  const id = status?.active_node_id ?? null;
  return id === null ? "No node" : nodeLabel(nodes, id);
}

/**
 * The live probe, only when it is about the active node: right after a switch the frame can still
 * describe the node before it, and its latency, egress and health must not be shown as the new one's.
 */
export function probeFor(frame: TrafficFrame | null | undefined, activeId: number | null | undefined): ActiveProbe {
  const probe = frame?.active ?? null;
  return probe !== null && activeId !== null && activeId !== undefined && probe.node_id === activeId ? probe : null;
}

/** The flag of the active node's egress, from its matched probe (see probeFor). */
export function activeFlag(probe: ActiveProbe): string {
  return probe ? flagEmoji(probe.egress_cc) : "";
}

/** "12 s" / "4 min" / "3 h" / "2 d" since a probe. */
export function probeAge(ageMs: number): string {
  const s = Math.max(0, Math.floor(ageMs / 1000));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  return h < 48 ? `${h} h` : `${Math.floor(h / 24)} d`;
}

/** Active row: "42 ms · live", "probe failed", or "health stale"; dimmed while the frame is not live. */
export function activeRow(node: Node | undefined, active: ActiveProbe, dim = false): LatencyRowData | null {
  if (!node) return null;
  const about = active && active.node_id === node.id ? active : null;
  const base = { id: node.id, name: node.name, flag: flagEmoji(about?.egress_cc), active: true, dim, age: null };
  if (!about || about.stale !== false) return { ...base, ms: null, state: "stale" };
  if (about.real_ok === false) return { ...base, ms: null, state: "failed" };
  if (about.real_ok !== true || about.latency_ms === null) return { ...base, ms: null, state: "stale" };
  return { ...base, ms: about.latency_ms, state: "live" };
}

const STATE_RANK: Record<LatencyRowData["state"], number> = { live: 0, measured: 0, stale: 1, failed: 1, "not-probed": 2 };

/**
 * Standby rows from the background sweep, lowest latency first, then failed, then never probed.
 * Shows last_real_ms, else last_tcp_ms, with the probe's age; dimmed past ten minutes.
 */
export function standbyRows(
  nodes: readonly Node[], health: readonly NodeHealth[], activeId: number | null | undefined, nowMs: number, limit = STANDBY_ROWS,
): LatencyRowData[] {
  const byNode = new Map(health.map((row) => [row.node_id, row]));
  const rows = nodes.filter((n) => n.id !== activeId).map((n): LatencyRowData => {
    const probe = byNode.get(n.id);
    const checked = probe?.checked_at ? Date.parse(probe.checked_at) : Number.NaN;
    const notProbed: LatencyRowData = { id: n.id, name: n.name, flag: "", ms: null, state: "not-probed", age: null, dim: false, active: false };
    if (!probe || !Number.isFinite(checked)) return notProbed;
    const ageMs = Math.max(0, nowMs - checked);
    const probed = { ...notProbed, flag: flagEmoji(probe.egress_cc), age: probeAge(ageMs), dim: ageMs > PROBE_DIM_MS };
    if (probe.last_real_ok === false) return { ...probed, state: "failed" };
    const ms = probe.last_real_ms ?? probe.last_tcp_ms;
    return ms === null ? notProbed : { ...probed, ms, state: "measured" };
  });
  rows.sort((a, b) => STATE_RANK[a.state] - STATE_RANK[b.state] || (a.ms ?? 0) - (b.ms ?? 0) || a.name.localeCompare(b.name));
  return rows.slice(0, limit);
}
