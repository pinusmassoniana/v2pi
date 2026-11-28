// Tunnel › Failover list: the priority list as rows, joined with node, subscription and health data. Pure and unit-tested.
import type { FailoverState, Node, NodeHealth, Subscription } from "../../api/client";
import type { PillTone } from "../../components/ui/Pill";

export interface FailoverRow {
  id: number;
  rank: number;
  name: string;
  source: string;
  active: boolean;
  tone: PillTone;
  state: string;
  /** Seconds of unbroken successful real checks, or null when the node is not currently passing. */
  healthyFor: number | null;
}

/** Where a node came from: its subscription's name, or "manual" for a server added by hand. */
export function nodeSource(node: Node, subs: readonly Subscription[]): string {
  if (node.subscription_id === null) return "manual";
  return subs.find((sub) => sub.id === node.subscription_id)?.name ?? "subscription";
}

function stateOf(health: NodeHealth | undefined): { tone: PillTone; state: string } {
  if (!health || health.last_real_ok === null) return { tone: "neutral", state: "not checked yet" };
  if (health.last_real_ok) return { tone: "ok", state: health.last_real_ms !== null ? `up · ${health.last_real_ms} ms` : "up" };
  return { tone: "bad", state: `down · ${health.fail_count} failed` };
}

export function failoverRows(
  failover: FailoverState, nodes: readonly Node[], health: readonly NodeHealth[], subs: readonly Subscription[],
  activeId: number | null, nowMs: number,
): FailoverRow[] {
  const rows: FailoverRow[] = [];
  for (const id of failover.order) {
    const node = nodes.find((n) => n.id === id);
    if (!node) continue;
    const since = failover.ok_since[String(id)];
    rows.push({
      id, rank: rows.length + 1, name: node.name, source: nodeSource(node, subs), active: id === activeId,
      ...stateOf(health.find((h) => h.node_id === id)),
      healthyFor: since === undefined ? null : Math.max(0, Math.round(nowMs / 1000 - since)),
    });
  }
  return rows;
}

/** "12 min" / "1 h 5 min" / "< 1 min". */
export function fmtHealthy(seconds: number): string {
  const min = Math.floor(seconds / 60);
  if (min < 1) return "< 1 min";
  return min < 60 ? `${min} min` : `${Math.floor(min / 60)} h ${min % 60} min`;
}

/** The order with one node moved by `delta` places (clamped). */
export function moved(order: readonly number[], id: number, delta: number): number[] {
  const from = order.indexOf(id);
  const to = Math.max(0, Math.min(order.length - 1, from + delta));
  const next = [...order];
  if (from < 0 || from === to) return next;
  next.splice(from, 1);
  next.splice(to, 0, id);
  return next;
}
