// Pure rules of Nodes › Servers: which group is shown, what the search keeps, how rows sort, when they can be
// reordered, how many render, and how a probe result reads. Unit-tested without rendering.
import type { Node, NodeHealth, Subscription } from "../../api/client";
import { SLOW_LATENCY_MS, type ActiveProbe } from "../../lib/nodeHealth";

/** The manual nodes' group: nodes that belong to no subscription. */
export const SERVERS = "servers";
/** A group: the manual Servers, or a subscription by id. */
export type GroupKey = typeof SERVERS | number;

export const SORT_KEYS = ["pos", "name", "address", "tcp", "http"] as const;
export type SortKey = (typeof SORT_KEYS)[number];
export type SortDir = "asc" | "desc";

/** Rows rendered before "show all"; selection, sort and probes still cover every row. */
export const ROW_CAP = 100;

/** The contract default: the first subscription, else Servers. */
export function defaultGroup(subs: readonly Subscription[]): GroupKey {
  return subs[0]?.id ?? SERVERS;
}

/** The group a URL names, or null when it names none at all. */
export function parseGroup(param: string | number | undefined): GroupKey | null {
  if (param === SERVERS) return SERVERS;
  const id = typeof param === "number" ? param : typeof param === "string" && /^\d+$/.test(param) ? Number(param) : Number.NaN;
  return Number.isInteger(id) && id > 0 ? id : null;
}

export interface ResolvedGroup {
  group: GroupKey;
  /** The URL named a group that does not exist (any more): replace it with the default. */
  replace: boolean;
}

/**
 * N1: the group to show for a URL's `group`. Until the subscriptions have loaded a subscription id is taken on
 * trust; after that an unknown one resolves to the default, and so does a missing one (without a replace).
 */
export function resolveGroup(param: string | number | undefined, subs: readonly Subscription[] | undefined): ResolvedGroup {
  const parsed = parseGroup(param);
  if (subs === undefined) return { group: parsed ?? SERVERS, replace: false };
  if (parsed === SERVERS || (parsed !== null && subs.some((s) => s.id === parsed))) return { group: parsed, replace: false };
  return { group: defaultGroup(subs), replace: param !== undefined };
}

/** The group a node belongs to. */
export function groupOf(node: Pick<Node, "subscription_id">): GroupKey {
  return node.subscription_id ?? SERVERS;
}

export function inScope(node: Pick<Node, "subscription_id">, group: GroupKey): boolean {
  return group === SERVERS ? node.subscription_id === null : node.subscription_id === group;
}

export interface GroupChip { key: GroupKey; label: string; count: number }

/** N1: one chip per subscription in their order, then Servers, each with its node count. */
export function groupChips(nodes: readonly Node[], subs: readonly Subscription[]): GroupChip[] {
  const counts = new Map<GroupKey, number>();
  for (const node of nodes) counts.set(groupOf(node), (counts.get(groupOf(node)) ?? 0) + 1);
  return [
    ...subs.map((s) => ({ key: s.id, label: s.name, count: counts.get(s.id) ?? 0 })),
    { key: SERVERS, label: "Servers", count: counts.get(SERVERS) ?? 0 },
  ];
}

/** The probe scope of a group: "servers" or the subscription id. */
export function probeScope(group: GroupKey): string {
  return group === SERVERS ? SERVERS : String(group);
}

/** connectBest's argument: null for the manual Servers. */
export function bestScope(group: GroupKey): number | null {
  return group === SERVERS ? null : group;
}

/** N2: case-insensitive substring over name, address and note. */
export function matchesSearch(node: Pick<Node, "name" | "address" | "note">, q: string): boolean {
  const needle = q.trim().toLowerCase();
  return needle === "" || `${node.name} ${node.address} ${node.note}`.toLowerCase().includes(needle);
}

export function healthById(health: readonly NodeHealth[] | undefined): Map<number, NodeHealth> {
  return new Map((health ?? []).map((row) => [row.node_id, row]));
}

function latency(health: ReadonlyMap<number, NodeHealth>, id: number, key: "last_tcp_ms" | "last_http_ms"): number {
  return health.get(id)?.[key] ?? Number.POSITIVE_INFINITY;
}

/**
 * N3: `pos` keeps the backend order (position, id). Name and address compare with localeCompare; TCP and HTTP by
 * their last reading, where a node without one counts as Infinity — last ascending, first descending.
 */
export function sortNodes(nodes: readonly Node[], health: ReadonlyMap<number, NodeHealth>, sort: SortKey, dir: SortDir): Node[] {
  if (sort === "pos") return [...nodes];
  const sign = dir === "desc" ? -1 : 1;
  const compare = (a: Node, b: Node): number => {
    if (sort === "name") return a.name.localeCompare(b.name);
    if (sort === "address") return a.address.localeCompare(b.address);
    const key = sort === "tcp" ? "last_tcp_ms" : "last_http_ms";
    const x = latency(health, a.id, key);
    const y = latency(health, b.id, key);
    return x === y ? 0 : x < y ? -1 : 1;
  };
  return [...nodes].sort((a, b) => compare(a, b) * sign);
}

/** The rows of a group after search and sort: what N9, N18 and N19 work over. */
export function shownNodes(
  nodes: readonly Node[], health: ReadonlyMap<number, NodeHealth>, group: GroupKey, q: string, sort: SortKey, dir: SortDir,
): Node[] {
  return sortNodes(nodes.filter((node) => inScope(node, group) && matchesSearch(node, q)), health, sort, dir);
}

/** N11: reordering only makes sense over the whole manual group in position order. */
export function canReorder(group: GroupKey, sort: SortKey, q: string): boolean {
  return group === SERVERS && sort === "pos" && q.trim() === "";
}

/** N11: swap the row at `index` with its neighbour; the whole id list to send, or null past either end. */
export function moveWithin(ids: readonly number[], index: number, delta: -1 | 1): number[] | null {
  const target = index + delta;
  if (index < 0 || index >= ids.length || target < 0 || target >= ids.length) return null;
  const next = [...ids];
  [next[index], next[target]] = [next[target]!, next[index]!];
  return next;
}

/** N19: the first ROW_CAP rows unless "show all" was chosen. */
export function visibleRows<T>(rows: readonly T[], showAll: boolean, cap = ROW_CAP): readonly T[] {
  return showAll || rows.length <= cap ? rows : rows.slice(0, cap);
}

/** N18: the selection, keeping only rows still shown. Returns the same set when nothing was dropped. */
export function pruneSelection(selected: ReadonlySet<number>, shown: readonly Pick<Node, "id">[]): ReadonlySet<number> {
  const ids = new Set(shown.map((node) => node.id));
  for (const id of selected) {
    if (!ids.has(id)) return new Set([...selected].filter((kept) => ids.has(kept)));
  }
  return selected;
}

/** N18: the select-all checkbox — none, some (indeterminate) or all of the shown rows. */
export function selectionState(selected: ReadonlySet<number>, shown: readonly Pick<Node, "id">[]): "none" | "some" | "all" {
  const count = shown.filter((node) => selected.has(node.id)).length;
  if (count === 0) return "none";
  return count === shown.length ? "all" : "some";
}

export type PillState = "ok" | "slow" | "failed" | "unknown";

/** N5 pills: ok, slow above 150 ms, failed, or unknown without a result. */
export function pillTone(ok: boolean | null | undefined, ms: number | null | undefined): PillState {
  if (ok === false) return "failed";
  if (ok !== true) return "unknown";
  return ms !== null && ms !== undefined && ms > SLOW_LATENCY_MS ? "slow" : "ok";
}

/** A probe result replacing the node's previous one in the health list (a single test, or one step of Test all). */
export function mergeHealth(rows: readonly NodeHealth[] | undefined, row: NodeHealth): NodeHealth[] {
  const list = rows ?? [];
  return list.some((old) => old.node_id === row.node_id) ? list.map((old) => (old.node_id === row.node_id ? row : old)) : [...list, row];
}

export interface Reading { label: "real" | "HTTP" | "TCP"; ms: number }

/** The phone card's one number: the real check's latency, else HTTP's, else TCP's — only a passing check counts. */
export function bestReading(health: NodeHealth | undefined): Reading | null {
  if (!health) return null;
  if (health.last_real_ok === true && health.last_real_ms !== null) return { label: "real", ms: health.last_real_ms };
  if (health.last_http_ok === true && health.last_http_ms !== null) return { label: "HTTP", ms: health.last_http_ms };
  if (health.last_tcp_ok === true && health.last_tcp_ms !== null) return { label: "TCP", ms: health.last_tcp_ms };
  return null;
}

/**
 * N5 on the active row: what the live probe says — its latency, "failed", or null when it says nothing fresh. Pass
 * the probe matched to the active node (probeFor), never the frame's raw `active`.
 */
export function liveReading(probe: ActiveProbe): number | "failed" | null {
  if (!probe || probe.stale !== false) return null;
  if (probe.real_ok === false) return "failed";
  return probe.real_ok === true ? probe.latency_ms : null;
}

/** N15: the delete confirmation, word for word. */
export function deleteNodeMessage(node: Pick<Node, "name" | "address">): string {
  return `Delete server "${node.name}" (${node.address})?`;
}

/** N11: the node list with one group's nodes put in `ids` order, every other node where it was. */
export function applyOrder(nodes: readonly Node[], ids: readonly number[]): Node[] {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const moved = new Set(ids);
  const queue = ids.map((id) => byId.get(id)).filter((node): node is Node => node !== undefined);
  let next = 0;
  return nodes.map((node) => (moved.has(node.id) && next < queue.length ? queue[next++]! : node));
}
