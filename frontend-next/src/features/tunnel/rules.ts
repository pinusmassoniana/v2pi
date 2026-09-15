// Tunnel › Routing rules: the staged ruleset, what counts as a change, what Save sends, the inline row checks, JSON
// import and the destination tester. Pure and unit-tested; no rendering here.
import type { Routing, RoutingIn, RoutingRuleIn } from "../../api/client";
import { inIPv4Cidr, parseDestination } from "../../lib/routing";

export const RULE_TYPES = ["geoip", "geosite", "domain", "ip", "port"] as const;
export type RuleType = (typeof RULE_TYPES)[number];
export const RULE_ACTIONS = ["direct", "proxy", "block"] as const;
export type RuleAction = (typeof RULE_ACTIONS)[number];
export const DOMAIN_STRATEGIES = ["IPIfNonMatch", "AsIs", "IPOnDemand"] as const;
export type DomainStrategy = (typeof DOMAIN_STRATEGIES)[number];

/** backend _MAX_RULES and _MAX_FIELD (value and label). */
export const MAX_RULES = 256;
export const MAX_RULE_FIELD = 512;

/** Geo suggestions for geoip / geosite values (a client list; the gateway's geo files decide what exists). */
export const GEO_TOKENS = ["ru", "cn", "private", "category-ru", "category-ads-all", "geolocation-!cn", "google", "telegram"] as const;

export const VALUE_PLACEHOLDERS: Readonly<Record<RuleType, string>> = {
  geoip: "ru | private | cn (comma-sep ok)",
  geosite: "category-ads-all",
  domain: "example.com, *.ya.ru",
  ip: "1.2.3.0/24, 10.0.0.0/8",
  port: "443 | 1000-2000 | 80,443",
};

export const RULES_FOOTNOTE = "DNS / QUIC / stats rules are injected automatically when those features are enabled.";
export const NO_RULES = "no rules — all traffic follows the default action";
export const BLOCK_DEFAULT_CONFIRM = "Default action is BLOCK — all non-matching traffic from the segment will be dropped. Continue?";
export const RESET_CONFIRM = "Reset to the default ruleset (no rules, default = proxy)?";
export const DISCARD_STAGED_CONFIRM = "Discard staged rules?";
export const TESTER_SUBTITLE = "how would this host/port be routed? (literal rules only — geo not evaluated locally)";
export const TESTER_PLACEHOLDER = "example.com  |  1.2.3.4  |  1.2.3.4:443";
export const IPV6_NOT_EVALUATED = "IPv6 preview is not evaluated locally — Validate/Save uses Xray's matcher.";

/** One editable rule. `key` is the client's stable row key; `id` the server's, null until the row is saved. */
export interface RuleRow {
  key: string;
  id: number | null;
  type: RuleType;
  value: string;
  action: RuleAction;
  enabled: boolean;
  label: string;
}

/** The ruleset as the editor holds it. */
export interface StagedRouting {
  rows: RuleRow[];
  defaultAction: RuleAction;
  domainStrategy: DomainStrategy;
}

let unsavedRows = 0;

/** A key for a row the server has not saved yet: unique for the session, never a saved row's `s‹id›`. */
export function newRowKey(): string {
  unsavedRows += 1;
  return `n${unsavedRows}`;
}

/**
 * The editor's state for a ruleset. A saved rule keeps a key made from its id, so the same server data always builds
 * the same rows; a rule without an id (a preset's new rules come back with id 0) gets a fresh key.
 */
export function stagedFromRouting(routing: Routing): StagedRouting {
  return {
    rows: routing.rules.map((rule) => {
      const id = rule.id > 0 ? rule.id : null;
      return {
        key: id === null ? newRowKey() : `s${id}`,
        id,
        type: rule.type as RuleType,
        value: rule.value,
        action: rule.action as RuleAction,
        enabled: rule.enabled,
        label: rule.label,
      };
    }),
    defaultAction: routing.default_action as RuleAction,
    domainStrategy: (routing.domain_strategy || "IPIfNonMatch") as DomainStrategy,
  };
}

/** R4: a preset reply (stored rules plus the preset's) replaces the staged rules, default and strategy. */
export const applyPreset = stagedFromRouting;

/** R6 Reset: no rules, default proxy, IPIfNonMatch — staged, not saved. */
export function resetRouting(): StagedRouting {
  return { rows: [], defaultAction: "proxy", domainStrategy: "IPIfNonMatch" };
}

function sameRule(a: RuleRow, b: RuleRow): boolean {
  return a.type === b.type && a.value === b.value && a.action === b.action && a.enabled === b.enabled && a.label === b.label;
}

/** Whether `current` differs from `base` in anything the editor holds: rows (with their order and ids), default, strategy. */
export function isStaged(base: StagedRouting, current: StagedRouting): boolean {
  if (base.defaultAction !== current.defaultAction || base.domainStrategy !== current.domainStrategy) return true;
  if (base.rows.length !== current.rows.length) return true;
  return base.rows.some((row, index) => {
    const other = current.rows[index]!;
    return row.id !== other.id || !sameRule(row, other);
  });
}

const filled = (row: RuleRow) => row.value.trim() !== "";

/**
 * R5: the "N changes" of the staged banner. Each row counts at most once, and a row with an empty value never counts
 * (Save drops it). Counted: an added row with a value; a saved row that is gone — or emptied, since Save drops it; a
 * saved row whose type, value, action, on/off or label changed; a saved row whose place among the saved rules changed
 * (a single move shifts two rows, so it counts two). Plus one each for a changed default action or domain strategy.
 */
export function changeCount(base: StagedRouting, current: StagedRouting): number {
  let count = 0;
  const kept = current.rows.filter(filled);
  const keptById = new Map(kept.filter((row) => row.id !== null).map((row) => [row.id!, row]));
  count += kept.filter((row) => row.id === null).length;
  const savedIds = new Set(base.rows.filter((row) => row.id !== null).map((row) => row.id!));
  count += [...savedIds].filter((id) => !keptById.has(id)).length;

  const baseOrder = base.rows.filter((row) => row.id !== null && keptById.has(row.id)).map((row) => row.id!);
  const currentOrder = kept.filter((row) => row.id !== null && savedIds.has(row.id)).map((row) => row.id!);
  for (const [index, id] of currentOrder.entries()) {
    const before = base.rows.find((row) => row.id === id)!;
    const after = keptById.get(id)!;
    const changed = before.type !== after.type || before.value.trim() !== after.value.trim() || before.action !== after.action
      || before.enabled !== after.enabled || before.label !== after.label;
    if (changed || baseOrder[index] !== id) count += 1;
  }
  if (base.defaultAction !== current.defaultAction) count += 1;
  if (base.domainStrategy !== current.domainStrategy) count += 1;
  return count;
}

/** "STAGED · 1 change" / "STAGED · 3 changes". */
export function changesLabel(count: number): string {
  return `${count} change${count === 1 ? "" : "s"}`;
}

/**
 * What Save and Validate send: values trimmed, empty rows dropped, and rows repeating an earlier (type, value, action)
 * dropped too, keeping the first. `dropped` counts those duplicates.
 */
export function toRoutingIn(state: StagedRouting): { body: RoutingIn; dropped: number } {
  const seen = new Set<string>();
  const rules: RoutingRuleIn[] = [];
  let dropped = 0;
  for (const row of state.rows) {
    const value = row.value.trim();
    if (!value) continue;
    const identity = JSON.stringify([row.type, value, row.action]);
    if (seen.has(identity)) {
      dropped += 1;
      continue;
    }
    seen.add(identity);
    rules.push({ type: row.type, value, action: row.action, enabled: row.enabled, label: row.label });
  }
  return { body: { rules, default_action: state.defaultAction, domain_strategy: state.domainStrategy }, dropped };
}

/** R2 Add: a proxied domain rule with no value yet, at the end. */
export function addRule(state: StagedRouting, key: string): StagedRouting {
  return { ...state, rows: [...state.rows, { key, id: null, type: "domain", value: "", action: "proxy", enabled: true, label: "" }] };
}

export type RulePatch = Partial<Pick<RuleRow, "type" | "value" | "action" | "enabled" | "label">>;

export function updateRule(state: StagedRouting, key: string, patch: RulePatch): StagedRouting {
  return { ...state, rows: state.rows.map((row) => (row.key === key ? { ...row, ...patch } : row)) };
}

export function removeRule(state: StagedRouting, key: string): StagedRouting {
  return { ...state, rows: state.rows.filter((row) => row.key !== key) };
}

/** R2 Move: swap with the neighbour above (-1) or below (+1); nothing past either end. */
export function moveRule(state: StagedRouting, key: string, delta: -1 | 1): StagedRouting {
  const from = state.rows.findIndex((row) => row.key === key);
  const to = from + delta;
  if (from < 0 || to < 0 || to >= state.rows.length) return state;
  const rows = [...state.rows];
  [rows[from], rows[to]] = [rows[to]!, rows[from]!];
  return { ...state, rows };
}

/** A rule value's tokens, as the backend splits it: on commas and newlines, trimmed, empty ones dropped. */
export function ruleTokens(value: string): string[] {
  return value.split(/[,\n]/).map((token) => token.trim()).filter(Boolean);
}

const OCTET = /^(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)$/;

/** A dotted IPv4 address as a number, as Python's ipaddress reads one (no leading zeros); null otherwise. */
export function ipv4Number(text: string): number | null {
  const parts = text.split(".");
  if (parts.length !== 4 || !parts.every((part) => OCTET.test(part))) return null;
  return parts.reduce((total, part) => total * 256 + Number(part), 0);
}

function prefixOf(text: string | undefined, max: number): number | null {
  if (text === undefined) return max;
  if (!/^\d{1,3}$/.test(text)) return null;
  const prefix = Number(text);
  return prefix <= max ? prefix : null;
}

/** An IPv4 address or CIDR as its first and last address; null when it is neither. Host bits are allowed (strict=False). */
export function ipv4Span(text: string): { first: number; last: number } | null {
  const [address, prefixText, ...rest] = text.split("/");
  if (rest.length > 0) return null;
  const base = ipv4Number(address!);
  const prefix = prefixOf(prefixText, 32);
  if (base === null || prefix === null) return null;
  const size = 2 ** (32 - prefix);
  const first = Math.floor(base / size) * size;
  return { first, last: first + size - 1 };
}

const HEX_GROUP = /^[0-9a-f]{1,4}$/i;

function ipv6Address(text: string): boolean {
  let groups = text;
  let extra = 0;
  const lastColon = groups.lastIndexOf(":");
  if (groups.includes(".")) {
    if (ipv4Number(groups.slice(lastColon + 1)) === null) return false;
    groups = `${groups.slice(0, lastColon + 1)}0`;
    extra = 1;
  }
  const halves = groups.split("::");
  if (halves.length > 2) return false;
  const parts = halves.map((half) => (half === "" ? [] : half.split(":")));
  if (!parts.every((list) => list.every((group) => HEX_GROUP.test(group)))) return false;
  const count = parts.flat().length + extra;
  return halves.length === 2 ? count <= 7 : count === 8;
}

/** An IPv6 address or CIDR (prefix 0–128). */
export function isIPv6Network(text: string): boolean {
  const [address, prefixText, ...rest] = text.split("/");
  return rest.length === 0 && address!.includes(":") && ipv6Address(address!) && prefixOf(prefixText, 128) !== null;
}

/**
 * The IPv4 ranges the backend's `_is_private_net` treats as private (Python 3.13 ipaddress: is_private, is_loopback or
 * is_link_local), which xray's built-in `geoip:private → direct` rule always matches first. 100.64.0.0/10 is not one.
 */
export const PRIVATE_IPV4_RANGES = [
  "0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
  "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "240.0.0.0/4", "255.255.255.255/32",
] as const;

/** Inside 192.0.0.0/24 but not private. */
export const PRIVATE_IPV4_EXCEPTIONS = ["192.0.0.9", "192.0.0.10"] as const;

const PRIVATE_SPANS = PRIVATE_IPV4_RANGES.map((range) => ipv4Span(range)!);
const EXCEPTION_NUMBERS = PRIVATE_IPV4_EXCEPTIONS.map((address) => ipv4Number(address)!);

/**
 * Whether an IPv4 address or CIDR sits wholly inside one private range, as Python decides for a network: its first and
 * last address both in the range, and neither of them an exception.
 */
export function isPrivateIPv4(text: string): boolean {
  const span = ipv4Span(text);
  if (!span) return false;
  if (EXCEPTION_NUMBERS.includes(span.first) || EXCEPTION_NUMBERS.includes(span.last)) return false;
  return PRIVATE_SPANS.some((range) => range.first <= span.first && span.last <= range.last);
}

/** Why a private range with an action other than direct can never fire. */
export const SHADOWED_BY_PRIVATE = "the built-in private → direct rule is matched first, so only direct works";

/**
 * The backend's structural checks on one rule, for an inline hint under its value; null when it passes. The gateway
 * stays the judge — Validate and Save run the real checks. IPv6 private ranges are left to it.
 */
export function validateRuleRow(row: Pick<RuleRow, "type" | "value" | "action">): string | null {
  const tokens = ruleTokens(row.value);
  if (tokens.length === 0) return "value required";
  if (row.type === "port") {
    for (const token of tokens) {
      const match = /^(\d{1,5})(?:-(\d{1,5}))?$/.exec(token);
      const low = match ? Number(match[1]) : 0;
      const high = match?.[2] !== undefined ? Number(match[2]) : low;
      if (!match || low < 1 || high > 65535 || low > high) return `bad port "${token}" — use 443, 1000-2000 or 80,443 within 1–65535`;
    }
  }
  if (row.type === "ip") {
    for (const token of tokens) {
      if (ipv4Span(token) === null && !isIPv6Network(token)) return `bad ip/cidr "${token}"`;
    }
  }
  if (row.action !== "direct") {
    if (row.type === "ip") {
      const privateToken = tokens.find((token) => isPrivateIPv4(token));
      if (privateToken) return `"${privateToken}" is a private range — ${SHADOWED_BY_PRIVATE}`;
    }
    if (row.type === "geoip" && tokens.some((token) => token.toLowerCase() === "private")) return `geoip:private — ${SHADOWED_BY_PRIVATE}`;
  }
  return null;
}

export type ImportResult = { ok: true; state: StagedRouting } | { ok: false; error: "invalid JSON" | "no rules array / bad rule shape" };

const BAD_SHAPE = "no rules array / bad rule shape";

function isOneOf<T extends string>(list: readonly T[], value: unknown): value is T {
  return typeof value === "string" && (list as readonly string[]).includes(value);
}

/**
 * R7 Import: an array of rules or `{rules: [...]}`, replacing the staged rules. The default action and domain strategy
 * change only when the document has them. Every rule needs a known type and action and a string value; `enabled` and
 * `label` are optional.
 */
export function importJson(text: string, current: StagedRouting): ImportResult {
  let doc: unknown;
  try {
    doc = JSON.parse(text);
  } catch {
    return { ok: false, error: "invalid JSON" };
  }
  const object = doc !== null && typeof doc === "object" && !Array.isArray(doc) ? (doc as Record<string, unknown>) : null;
  const list = Array.isArray(doc) ? doc : object?.rules;
  if (!Array.isArray(list)) return { ok: false, error: BAD_SHAPE };
  const rows: RuleRow[] = [];
  for (const item of list) {
    const rule = item !== null && typeof item === "object" ? (item as Record<string, unknown>) : null;
    if (!rule || !isOneOf(RULE_TYPES, rule.type) || !isOneOf(RULE_ACTIONS, rule.action) || typeof rule.value !== "string"
      || (rule.enabled !== undefined && typeof rule.enabled !== "boolean") || (rule.label !== undefined && typeof rule.label !== "string")) {
      return { ok: false, error: BAD_SHAPE };
    }
    rows.push({
      key: newRowKey(), id: null, type: rule.type, value: rule.value, action: rule.action,
      enabled: (rule.enabled as boolean | undefined) ?? true, label: (rule.label as string | undefined) ?? "",
    });
  }
  const defaultAction = object?.default_action;
  const domainStrategy = object?.domain_strategy;
  if ((defaultAction !== undefined && !isOneOf(RULE_ACTIONS, defaultAction)) || (domainStrategy !== undefined && !isOneOf(DOMAIN_STRATEGIES, domainStrategy))) {
    return { ok: false, error: BAD_SHAPE };
  }
  return {
    ok: true,
    state: {
      rows,
      defaultAction: defaultAction ?? current.defaultAction,
      domainStrategy: domainStrategy ?? current.domainStrategy,
    },
  };
}

/** R7 Export: the ruleset as Save would send it, as 2-space JSON. */
export function exportJson(state: StagedRouting): string {
  return JSON.stringify(toRoutingIn(state).body, null, 2);
}

/** A destination tester answer: the action it would take (null when not evaluated) and the words after it. */
export interface TesterResult {
  action: RuleAction | null;
  detail: string;
}

function portMatches(token: string, port: number): boolean {
  const [low, high] = token.split("-").map(Number);
  return high === undefined ? port === low : port >= low! && port <= high;
}

/**
 * R8: where a destination would go under the staged rules, evaluated here without the gateway. A private IPv4 goes
 * direct before any rule; then the rules in order, skipping switched-off and empty ones — domain (exact or a suffix, a
 * leading `*.` ignored), ip (IPv4 CIDRs), port (a port, a range or a list) — first match wins; geo rules are not
 * evaluated. Null for an empty input.
 */
export function testDestination(input: string, state: StagedRouting): TesterResult | null {
  const raw = input.trim();
  if (!raw) return null;
  const parsed = parseDestination(raw);
  if (parsed.ipv6) return { action: null, detail: IPV6_NOT_EVALUATED };
  const { host, port } = parsed;
  const isIp = ipv4Number(host) !== null;
  if (isIp && isPrivateIPv4(host)) return { action: "direct", detail: "(private range, always matched first)" };
  let skippedGeo = false;
  for (const row of state.rows) {
    if (!row.enabled || !row.value.trim()) continue;
    const tokens = ruleTokens(row.value);
    let hit: boolean;
    if (row.type === "domain") {
      hit = tokens.some((token) => {
        const base = token.replace(/^\*\.?/, "");
        return host === base || host.endsWith(`.${base}`);
      });
    } else if (row.type === "ip") {
      hit = isIp && tokens.some((token) => inIPv4Cidr(host, token));
    } else if (row.type === "port") {
      hit = port !== null && tokens.some((token) => portMatches(token, port));
    } else {
      skippedGeo = true;
      continue;
    }
    if (hit) {
      return { action: row.action, detail: `(matched ${row.type} "${row.value.trim()}"${row.label ? ` · ${row.label}` : ""})` };
    }
  }
  return { action: state.defaultAction, detail: `(default${skippedGeo ? " · geo rules not evaluated locally" : ""})` };
}
