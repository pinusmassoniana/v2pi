// Pure derivations for Home › Overview and Home › Traffic: every rule that turns API data into what
// the screens say lives here, where it is unit-tested without rendering anything.
import type { ConnEvent, NetworkSegment, Node, Routing, Status, TrafficFrame } from "../../api/client";
import { LIVE_WINDOW_MAX_SEC } from "../../api/cadence";
import type { TrafficSample } from "../../api/traffic";
import type { EventLevel, NodeSlot, OutboundRates, PathLeg, PathRate, Tone } from "../../components/data/types";
import { agoLabel } from "../../lib/dashboard";
import { fmtRate, formatUriHost } from "../../lib/format";
import { SLOW_LATENCY_MS, connectedState, type ActiveProbe } from "../../lib/nodeHealth";

// The node-health rules Home shares with Nodes live in src/lib/nodeHealth.ts; Home's screens keep importing them from here.
export {
  PROBE_DIM_MS, STANDBY_ROWS, activeFlag, activeNode, activeNodeLabel, activeRow, nodeLabel, probeAge, probeFor, standbyRows,
} from "../../lib/nodeHealth";

export interface Labelled<L extends string> { label: L; tone: Tone }

/** The auto-failover alert stays for a day unless dismissed. */
export const FAILOVER_ALERT_SEC = 86_400;
/** Same key as the Svelte panel, so a dismissal survives the switch to this one. */
export const FAILOVER_DISMISSED_KEY = "failoverDismissed";
export const RECENT_EVENTS = 6;
export const FAILOVER_HISTORY = 8;
export const ROUTING_SUMMARY_RULES = 4;

// ---- status block ----------------------------------------------------------------------------

/** O4 xray: RUNNING if running; RECONNECTING while the supervisor reports an error; STOPPED; "—" before any status. */
export function xrayLabel(status: Status | undefined): Labelled<"RUNNING" | "RECONNECTING" | "STOPPED" | "—"> {
  if (status?.running) return { label: "RUNNING", tone: "ok" };
  if (status?.xray_state === "error") return { label: "RECONNECTING", tone: "warn" };
  if (status?.running === false) return { label: "STOPPED", tone: "bad" };
  return { label: "—", tone: "neutral" };
}

/** O1: only a proven drift is a problem. "unknown" (nothing started yet) and a missing field are not. */
export function hasConfigDrift(status: Status | undefined): boolean {
  return status?.config_drift === "drift";
}

export type TunnelLabel = Labelled<"ONLINE" | "UNKNOWN" | "OFFLINE">;

/**
 * O4 tunnel — the one answer the status block, the upstream health pill, Traffic's uptime and the topbar all
 * show. Nothing is known while the status poll fails or before its first answer, whatever the last one said.
 * OFFLINE with no active node, with xray stopped, or when a fresh probe of the active node failed; UNKNOWN while
 * health is not fresh; ONLINE when the gateway says so; UNKNOWN otherwise. `probe` is the live probe matched to
 * the active node (probeFor).
 */
export function tunnelLabel(status: Status | undefined, statusError: boolean, probe: ActiveProbe): TunnelLabel {
  const connected = connectedState(status, statusError);
  if (connected === "unknown" || !status) return { label: "UNKNOWN", tone: "neutral" };
  if (status.active_node_id === null || connected === "not running") return { label: "OFFLINE", tone: "bad" };
  const matched = probe && probe.node_id === status.active_node_id ? probe : null;
  if (matched?.stale === false && matched.real_ok === false) return { label: "OFFLINE", tone: "bad" };
  if (matched?.stale === true || status.active_health_fresh === false) return { label: "UNKNOWN", tone: "neutral" };
  return status.tunnel_online === true ? { label: "ONLINE", tone: "ok" } : { label: "UNKNOWN", tone: "neutral" };
}

function ipv4(address: string): number[] | null {
  const parts = address.trim().split(".");
  if (parts.length !== 4 || !parts.every((part) => /^\d{1,3}$/.test(part))) return null;
  const octets = parts.map(Number);
  return octets.every((octet) => octet <= 255) ? octets : null;
}

/** DHCP pool size from dhcp_start–dhcp_end on one /24; null when the range cannot be read that way. */
export function poolSize(segment: Pick<NetworkSegment, "dhcp_start" | "dhcp_end"> | undefined): number | null {
  const start = segment ? ipv4(segment.dhcp_start) : null;
  const end = segment ? ipv4(segment.dhcp_end) : null;
  if (!start || !end) return null;
  if (start[0] !== end[0] || start[1] !== end[1] || start[2] !== end[2]) return null;
  return end[3]! >= start[3]! ? end[3]! - start[3]! + 1 : null;
}

const SECURITY: Record<string, string> = { reality: "Reality", tls: "TLS", none: "no TLS" };

/** "VLESS · Reality · host:443" under the node name. */
export function nodeEndpoint(node: Node): string {
  const security = SECURITY[node.security] ?? (node.security || "no TLS");
  return `VLESS · ${security} · ${formatUriHost(node.address)}:${node.port}`;
}

/** Said when a confirmed rollback no longer matches what the gateway offers. */
export const ROLLBACK_TARGET_CHANGED = "The rollback target changed — try again";

/**
 * O12 (§12.5): the gateway says a rollback would work, and it would still go back to `prevId` — the node the user
 * was asked to confirm. `prev_active_node_id` alone only names that node; it is no promise the rollback succeeds.
 */
export function rollbackStillValid(status: Status | undefined, prevId: number | null): boolean {
  return status?.rollback_available === true && status.prev_active_node_id === prevId;
}

// ---- live traffic ----------------------------------------------------------------------------

export interface SessionTotals { up: number; down: number; source: "session" | "lifetime" | "totals" }

/** O5: data since the last connect when the gateway reports it, else the durable total, else since xray start. */
export function sessionTotals(frame: TrafficFrame | null): SessionTotals | null {
  if (!frame) return null;
  if (frame.session) return { ...frame.session, source: "session" };
  if (frame.lifetime) return { ...frame.lifetime, source: "lifetime" };
  return frame.totals ? { ...frame.totals, source: "totals" } : null;
}

/** The last `windowMs` of one direction, thinned to at most `maxPoints` values, for a KPI sparkline. */
export function recentValues(samples: readonly TrafficSample[], windowMs: number, key: "up" | "down", maxPoints = 60): number[] {
  if (samples.length === 0) return [];
  const end = samples[samples.length - 1]!.ts;
  let first = samples.length;
  while (first > 0 && samples[first - 1]!.ts > end - windowMs) first--;
  const step = Math.max(1, Math.ceil((samples.length - first) / maxPoints));
  const values: number[] = [];
  for (let i = first; i < samples.length; i += step) values.push(samples[i]![key]);
  return values;
}

/** H1 / O6: the highest download sample, in one pass; null when nothing moved. */
export function peakOf(samples: readonly TrafficSample[]): { bps: number; ts: number } | null {
  let best: TrafficSample | null = null;
  for (const sample of samples) if (sample.down > 0 && (best === null || sample.down > best.down)) best = sample;
  return best ? { bps: best.down, ts: best.ts } : null;
}

/**
 * O6 (§12.3): dim the throughput chart as "tunnel health is stale" only when there is a tunnel to judge — an active
 * node whose matched live probe is not known fresh — and only on the live windows: recorded 24 h / 7 d history is
 * never judged by the current probe.
 */
export function chartStale(windowSec: number, activeId: number | null | undefined, probe: ActiveProbe): boolean {
  if (activeId === null || activeId === undefined || windowSec > LIVE_WINDOW_MAX_SEC) return false;
  return probe?.stale !== false;
}

/** O8 tunnel leg: off while xray is down or health is stale, bad on a failed real check, ok on a passing one. */
export function tunnelLeg(status: Status | undefined, active: ActiveProbe): PathLeg {
  if (!status?.running || !active || active.stale !== false) return "off";
  if (active.real_ok === false) return "bad";
  return active.real_ok === true ? "ok" : "off";
}

/** The live latency, only when it is fresh and the real check passed. */
export function liveLatency(active: ActiveProbe): number | null {
  return active && active.stale === false && active.real_ok === true ? active.latency_ms : null;
}

// ---- connection path -------------------------------------------------------------------------

/** O8: the tunnel's (proxy) and direct traffic's live down and up. A missing outbound is 0; no frame is null. */
export function outboundRates(frame: TrafficFrame | null): OutboundRates | null {
  if (!frame) return null;
  const rate = (name: "proxy" | "direct"): PathRate => ({
    down: frame.outbounds[name]?.down_bps ?? 0,
    up: frame.outbounds[name]?.up_bps ?? 0,
  });
  return { proxy: rate("proxy"), direct: rate("direct") };
}

/** O8 rate pill: "—" without a frame, "idle" when nothing moves either way, else one line per direction, each in its own unit. */
export function rateLines(rate: PathRate | null): string[] {
  if (rate === null) return ["—"];
  if (rate.down === 0 && rate.up === 0) return ["idle"];
  return [`↓ ${fmtRate(rate.down)}`, `↑ ${fmtRate(rate.up)}`];
}

const SLOT_OFF = { leg: "off", note: null, tone: "neutral", ms: null } as const;

/**
 * O8 node slot, the first matching row winning: no active node; xray stopped; no live frame (stats off, or none yet); a
 * failed real check; a passing one slower than SLOW_LATENCY_MS (exactly 150 ms is not slow); a passing one; and
 * anything else — a stale probe, a probe about another node, a check with no answer — is stale health. `probe` is the
 * live probe matched to the active node (probeFor).
 */
export function nodeHealthSlot(status: Status | undefined, frame: TrafficFrame | null, probe: ActiveProbe): NodeSlot {
  if (!status || status.active_node_id === null) return { ...SLOT_OFF, state: "none", text: "—" };
  if (!status.running) return { ...SLOT_OFF, state: "stopped", text: "xray stopped" };
  if (!frame) return { ...SLOT_OFF, state: "no-frame", text: "—" };
  const leg = tunnelLeg(status, probe);
  if (leg === "bad") return { state: "bad", leg, text: "check failed", note: null, tone: "bad", ms: null };
  if (leg === "off") return { ...SLOT_OFF, state: "stale", text: "health stale" };
  const ms = liveLatency(probe);
  if (ms !== null && ms > SLOW_LATENCY_MS) return { state: "slow", leg, text: `${ms} ms`, note: "slow", tone: "warn", ms };
  return { state: "ok", leg, text: ms === null ? "OK" : `${ms} ms`, note: null, tone: "ok", ms };
}

function rateWords(rate: PathRate | null): string {
  if (rate === null) return "rates unknown";
  return rate.down === 0 && rate.up === 0 ? "idle" : `down ${fmtRate(rate.down)}, up ${fmtRate(rate.up)}`;
}

export interface PathLabelInput {
  /** The active node's name without its leading flag. */
  name: string;
  slot: NodeSlot;
  rates: OutboundRates | null;
  /** killSwitchState's label. */
  killSwitch: string;
  /** The live frame stopped updating. */
  dim: boolean;
}

/** The tunnel in the path's name, in the latency pill's words: OFF only while nothing can carry it, never while health is just unknown. */
const TUNNEL_WORDS: Record<NodeSlot["state"], string> = {
  none: "OFF, no node",
  stopped: "OFF, xray stopped",
  "no-frame": "health unknown",
  stale: "health stale",
  bad: "DOWN, real check failed",
  slow: "slow",
  ok: "OK",
};

/** O8: the connection path's accessible name — the stops, the tunnel with its rates, direct traffic and the kill-switch. */
export function pathLabel({ name, slot, rates, killSwitch, dim }: PathLabelInput): string {
  const parts = [
    `Connection path: devices, gateway, ${name}, internet.`,
    `Tunnel ${TUNNEL_WORDS[slot.state]}${slot.ms === null ? "" : `, ${slot.ms} ms`}, ${rateWords(rates?.proxy ?? null)}.`,
    `Direct by routing rules: ${rateWords(rates?.direct ?? null)}.`,
    `Kill-switch ${killSwitch}.`,
  ];
  if (dim) parts.push("Live stats paused.");
  return parts.join(" ");
}

export interface LatencyStats { ms: number | null; avg: number | null; fresh: boolean; sub: string }

/** H1 active latency: the fresh number with the average of its history, else "stale" / "unknown" and the probe's age. */
export function latencyStats(active: ActiveProbe, nowMs: number): LatencyStats {
  const history = active?.lat_history ?? [];
  const avg = history.length ? Math.round(history.reduce((sum, ms) => sum + ms, 0) / history.length) : null;
  const ms = liveLatency(active);
  if (ms !== null) return { ms, avg, fresh: true, sub: avg === null ? "live" : `avg ${avg} ms · live` };
  const word = active?.stale ? "stale" : "unknown";
  const checked = active?.checked_at ? Date.parse(active.checked_at) : Number.NaN;
  return { ms: null, avg, fresh: false, sub: Number.isFinite(checked) ? `${word} · ${agoLabel(checked / 1000, nowMs / 1000)}` : word };
}

// ---- upstream health -------------------------------------------------------------------------

/** O7 pill: FAILOVER READY / NO ELIGIBLE STANDBY while online, UNKNOWN while the tunnel is, otherwise OFFLINE. */
export function failoverPill(
  status: Status | undefined, tunnel: TunnelLabel,
): Labelled<"FAILOVER READY" | "NO ELIGIBLE STANDBY" | "UNKNOWN" | "OFFLINE"> {
  if (tunnel.label === "UNKNOWN") return { label: "UNKNOWN", tone: "neutral" };
  if (tunnel.label === "OFFLINE") return { label: "OFFLINE", tone: "bad" };
  return status?.failover_ready ? { label: "FAILOVER READY", tone: "ok" } : { label: "NO ELIGIBLE STANDBY", tone: "warn" };
}

// ---- alerts ----------------------------------------------------------------------------------

/** O2: the failover timestamp to announce, or null — older than a day, or dismissed exactly that one. */
export function failoverBanner(lastFailoverAt: number | null | undefined, dismissed: number | null, nowMs: number): number | null {
  if (typeof lastFailoverAt !== "number" || lastFailoverAt <= 0 || lastFailoverAt === dismissed) return null;
  return nowMs / 1000 - lastFailoverAt < FAILOVER_ALERT_SEC ? lastFailoverAt : null;
}

/** The dismissed failover timestamp; null when none is stored or site data is blocked. */
export function readFailoverDismissed(): number | null {
  try {
    const raw = localStorage.getItem(FAILOVER_DISMISSED_KEY);
    const ts = Number(raw);
    return raw !== null && Number.isFinite(ts) && ts > 0 ? ts : null;
  } catch {
    return null;
  }
}

export function writeFailoverDismissed(ts: number): void {
  try {
    localStorage.setItem(FAILOVER_DISMISSED_KEY, String(ts));
  } catch {
    // blocked site data: the dismissal lasts until the page reloads
  }
}

// ---- summaries -------------------------------------------------------------------------------

export type RouteBadge = "proxy" | "direct" | "block";

export function routeBadge(action: string): RouteBadge {
  const a = action.toLowerCase();
  if (a.includes("block")) return "block";
  return a.includes("direct") ? "direct" : "proxy";
}

export interface RoutingSummary {
  rows: { id: number; badge: RouteBadge; text: string }[];
  /** "4 of 6 rules" */
  header: string;
  defaultBadge: RouteBadge;
  /** "→ nl-ams-03" when the default is to proxy; null otherwise (direct and block go nowhere near a node). */
  defaultTarget: string | null;
}

/** O9 (§12.4): the first four enabled rules by position, and the default action — towards the active node only when it proxies. */
export function routingSummary(routing: Routing, activeLabel: string): RoutingSummary {
  const enabled = routing.rules.filter((r) => r.enabled).sort((a, b) => a.position - b.position);
  const rows = enabled.slice(0, ROUTING_SUMMARY_RULES).map((r) => ({
    id: r.id, badge: routeBadge(r.action), text: r.type ? `${r.type}:${r.value}` : r.value,
  }));
  const defaultBadge = routeBadge(routing.default_action);
  return {
    rows,
    header: `${rows.length} of ${routing.rules.length} rules`,
    defaultBadge,
    defaultTarget: defaultBadge === "proxy" ? `→ ${activeLabel}` : null,
  };
}

// ---- events ----------------------------------------------------------------------------------

/** O11 level from the event kind. Bad is tested first, so "failover" is bad although it contains no "ok". */
export function eventLevel(kind: string): EventLevel {
  const k = kind.toLowerCase();
  if (/err|fail|drop|down|leak/.test(k)) return "bad";
  if (/warn|degrad|stale|retry|timeout/.test(k)) return "warn";
  if (/ok|up|recover|restore|connect|appl/.test(k)) return "ok";
  return "info";
}

function newestFirst(events: readonly ConnEvent[]): ConnEvent[] {
  return events
    .map((event, index) => ({ event, index }))
    .sort((a, b) => b.event.ts - a.event.ts || b.index - a.index)
    .map(({ event }) => event);
}

/** O11: the last six events, newest first. */
export function recentEvents(events: readonly ConnEvent[], limit = RECENT_EVENTS): ConnEvent[] {
  return newestFirst(events).slice(0, limit);
}

// "kill-switch" is the fail-closed guard switched on or off (Gateway › Network's Apply records it), not a node switch.
const FAILOVER_KIND = /failover|(?<!kill-)switch/i;

/** H5: the last eight failover / switch events, newest first; else one row from last_failover_at; else none. */
export function failoverHistory(events: readonly ConnEvent[], lastFailoverAt: number | null | undefined, limit = FAILOVER_HISTORY): ConnEvent[] {
  const matching = events.filter((event) => FAILOVER_KIND.test(event.kind));
  if (matching.length > 0) return newestFirst(matching).slice(0, limit);
  return typeof lastFailoverAt === "number" && lastFailoverAt > 0
    ? [{ ts: lastFailoverAt, kind: "failover", detail: "Auto-failover — the gateway switched node" }]
    : [];
}

const pad = (n: number) => String(n).padStart(2, "0");
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Local HH:MM:SS of an epoch-seconds timestamp. */
export function clockTime(epochSec: number): string {
  const d = new Date(epochSec * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** HH:MM:SS today, "13 Sep · 19:26:15" on an earlier (or later) local day. */
export function whenLabel(epochSec: number, nowMs: number): string {
  const d = new Date(epochSec * 1000);
  const now = new Date(nowMs);
  const sameDay = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  return sameDay ? clockTime(epochSec) : `${pad(d.getDate())} ${MONTHS[d.getMonth()]} · ${clockTime(epochSec)}`;
}

/** "11 Sep 10:02": when the current connection started. */
export function sinceLabel(epochSec: number): string {
  const d = new Date(epochSec * 1000);
  return `${pad(d.getDate())} ${MONTHS[d.getMonth()]} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
