// System › Panel, the settings file: what an export writes, and what an import is checked against before the confirm.
// Pure and unit-tested. Export never touches the network — it writes what the panel already has.
import type { Settings } from "../../api/client";
import { SETTINGS_REAPPLY_KEYS } from "../../api/client";
import { fmtBytes } from "../../lib/format";
import type { PreCheck } from "./backupFile";
import { PORT_RANGE_MESSAGE, PORT_RESERVED_MESSAGE, RESERVED_PORTS, SAMPLE_MAX_MESSAGE, SAMPLE_MIN_MESSAGE, integerMessage } from "./statsForm";

/** Everything else's body limit (app.py DEFAULT_BODY_LIMIT). */
export const MAX_SETTINGS_BYTES = 1024 * 1024;
export const SETTINGS_FILENAME = "v2pi-settings.json";

/** Every key `SettingsIn` accepts — which is every key the `Settings` type has. */
export const SETTINGS_KEYS = [
  "tunneled_fetch", "subs_auto_switch", "routing_default_action", "health_enabled", "health_sweep_enabled",
  "health_interval", "health_active_interval", "health_hysteresis", "health_probe_url", "failover_enabled",
  "failover_cooldown", "stats_enabled", "stats_api_port", "traffic_sample_ms", "dns_intercept",
  "session_timeout_min", "auto_backup_enabled", "update_check_enabled", "traffic_cap_gb", "traffic_cap_reset_day",
  "diag_url", "diag_bytes",
] as const satisfies readonly (keyof Settings)[];

/**
 * Routing owns it, so the file must not carry it: `SettingsIn` accepts `routing_default_action` and a PUT would store
 * it, but it is not one of `_SETTINGS_CONFIG_KEYS`, so the stored default would move while the live tunnel kept the
 * old one until something else re-applied. It is excluded from the reset keys for exactly the same reason.
 */
export const EXPORT_EXCLUDED = "routing_default_action" as const;

export type SettingsFile = Omit<Settings, typeof EXPORT_EXCLUDED>;

/** The cached settings minus the routing-owned key. No API call: this is what the panel already holds. */
export function exportSettings(settings: Settings): SettingsFile {
  const copy: Record<string, unknown> = { ...settings };
  delete copy[EXPORT_EXCLUDED];
  return copy as unknown as SettingsFile;
}

export function exportedText(settings: Settings): string {
  return JSON.stringify(exportSettings(settings), null, 2);
}

const BOOLEAN_KEYS = new Set<string>([
  "tunneled_fetch", "subs_auto_switch", "health_enabled", "health_sweep_enabled", "failover_enabled",
  "stats_enabled", "dns_intercept", "auto_backup_enabled", "update_check_enabled",
]);

/** The int floors the gateway enforces (config.py SETTINGS_INT_BOUNDS), with its own messages. */
const INT_BOUNDS: Record<string, [low: number, high: number | null]> = {
  traffic_cap_gb: [0, 1_000_000],
  traffic_cap_reset_day: [1, 28],
  health_interval: [60, null], health_active_interval: [10, null], health_hysteresis: [1, null],
  failover_cooldown: [0, null], session_timeout_min: [0, null], traffic_sample_ms: [500, 60_000], stats_api_port: [1, 65535],
};

/**
 * Why the gateway would refuse this key's value, in the gateway's own words — or null when it would not. Booleans are
 * accepted in the three shapes a settings row can hold (`true`, `1`, `"1"`), because pydantic accepts them too and a
 * stricter browser check would refuse a file the gateway would have taken.
 */
export function settingValueIssue(key: string, value: unknown): string | null {
  if (BOOLEAN_KEYS.has(key)) {
    const ok = typeof value === "boolean" || value === 0 || value === 1 || value === "0" || value === "1";
    return ok ? null : `${key} must be true or false`;
  }
  const bounds = INT_BOUNDS[key];
  if (bounds) {
    if (typeof value === "boolean") return integerMessage(key);
    const number = typeof value === "number" ? value : typeof value === "string" && /^-?\d+$/.test(value.trim()) ? Number(value.trim()) : null;
    if (number === null || !Number.isInteger(number)) return integerMessage(key);
    if (key === "stats_api_port") {
      if (number < 1 || number > 65535) return PORT_RANGE_MESSAGE;
      return (RESERVED_PORTS as readonly number[]).includes(number) ? PORT_RESERVED_MESSAGE : null;
    }
    if (key === "traffic_sample_ms") {
      if (number < 500) return SAMPLE_MIN_MESSAGE;
      if (number > 60_000) return SAMPLE_MAX_MESSAGE;
      return null;
    }
    const [low, high] = bounds;
    if (number < low) return `${key} must be >= ${low}`;
    if (high !== null && number > high) return `${key} must be <= ${high}`;
    return null;
  }
  if (key === "health_probe_url") {
    if (typeof value !== "string") return "health_probe_url must be a string";
    return value.length <= 2048 ? null : "health_probe_url must be at most 2048 characters";
  }
  if (key === "routing_default_action") return null;   // dropped before anything is sent
  return null;
}

export interface ImportChecks {
  checks: PreCheck[];
  /** The patch the import would send — the file minus the routing-owned key — or null while any check fails. */
  patch: Partial<Settings> | null;
  unknown: string[];
  /** Which of the four keys that rebuild the live tunnel this file carries. */
  reapplyKeys: string[];
}

function parsed(text: string): unknown {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return undefined;
  }
}

/**
 * Everything the browser can decide before the confirm. `SettingsIn` is strict: ONE unknown key refuses the whole
 * patch with an opaque 422, so the unknown ones are named here instead, and an explicit null gives its own 422 — so
 * they are named too. A file with no keys at all is a no-op the gateway would accept; it is refused here because
 * importing nothing is never what the operator meant — and (fix round 1) so is a file whose only keys are the
 * routing-owned one: `EXPORT_EXCLUDED` is dropped below, so a file holding only `routing_default_action` would
 * otherwise pass every check and still send an empty `PUT /settings` while claiming success.
 */
export function importChecks(text: string): ImportChecks {
  const value = parsed(text);
  const isObject = value !== null && typeof value === "object" && !Array.isArray(value);
  const shape: PreCheck = { ok: isObject, label: "parses as JSON, and is an object" };
  if (!isObject) return { checks: [shape], patch: null, unknown: [], reapplyKeys: [] };

  const doc = value as Record<string, unknown>;
  const keys = Object.keys(doc);
  const known = new Set<string>(SETTINGS_KEYS);
  const unknown = keys.filter((key) => !known.has(key));
  const nulls = keys.filter((key) => doc[key] === null);
  const sendable = keys.filter((key) => key !== EXPORT_EXCLUDED);
  const keysCheck: PreCheck = unknown.length
    ? { ok: false, label: `${unknown.length} field${unknown.length === 1 ? " is not a settings key" : "s are not settings keys"}: ${unknown.join(", ")}` }
    : { ok: sendable.length > 0, label: sendable.length ? `${keys.length} fields, all known settings keys` : "no settings in this file" };

  const issues = keys.filter((key) => known.has(key) && doc[key] !== null).map((key) => settingValueIssue(key, doc[key])).filter((issue): issue is string => issue !== null);
  const problems = [...nulls.map((key) => `${key} may be omitted but not null`), ...issues];
  const valuesCheck: PreCheck = problems.length
    ? { ok: false, label: problems.join(" · ") }
    : { ok: true, label: "every value passes the same checks the cards use" };

  const reapplyKeys = SETTINGS_REAPPLY_KEYS.filter((key) => key in doc && doc[key] !== null);
  const reapplyCheck: PreCheck = {
    ok: true,
    label: reapplyKeys.length
      ? `${reapplyKeys.length} of them rebuild the live tunnel: ${reapplyKeys.join(", ")}`
      : "none of them rebuild the live tunnel",
  };

  const checks = [shape, keysCheck, valuesCheck, reapplyCheck];
  if (!keysCheck.ok || !valuesCheck.ok) return { checks, patch: null, unknown, reapplyKeys: [...reapplyKeys] };
  const patch: Record<string, unknown> = { ...doc };
  delete patch[EXPORT_EXCLUDED];
  return { checks, patch: patch as Partial<Settings>, unknown, reapplyKeys: [...reapplyKeys] };
}

/** Said instead of sending, so an oversized file gets a sentence rather than a 413 raised before the session is read. */
export function settingsFileTooLarge(size: number): { title: string; text: string } | null {
  if (size <= MAX_SETTINGS_BYTES) return null;
  return {
    title: "That file is larger than 1 MB",
    text: `the gateway refuses it before it even looks at your session (${fmtBytes(size)}). Export a fresh settings file instead of editing an old one.`,
  };
}

export const IMPORT_CONFIRM =
  "Apply the settings in this file? Only the fields it contains are changed; the rest stay as they are. Unknown " +
  "fields are refused. This can rebuild the live tunnel.";

export const IMPORT_NOTE =
  "Only the fields the file contains are changed; the rest stay as they are. One unknown field refuses the whole file, not just that key.";

export const IMPORT_UNKNOWN_HINT =
  "Remove them, or export a fresh file from this panel — one unknown key refuses the whole import.";

export const EXPORT_INTRO =
  "Export writes what the panel already has — no request to the gateway. The routing default action is never in the " +
  "file: Routing owns it, and a stored change there would not reach the live tunnel.";

export function exportedMessage(): string {
  return `settings exported · ${SETTINGS_FILENAME}`;
}

export const IMPORTED_MESSAGE = "settings applied from file";

// --- the danger zone: what a reset actually writes ---

/**
 * `_SETTINGS_RESET_KEYS` with their `SETTINGS_DEFAULTS` values, in that order. Routing-owned keys and every
 * network / remote-access key are untouched, which is why three of these belong to other sections.
 */
export const RESET_KEYS: readonly (readonly [key: string, value: string])[] = [
  ["tunneled_fetch", "1"], ["subs_auto_switch", "1"], ["dns_intercept", "0"], ["health_enabled", "1"],
  ["health_sweep_enabled", "1"], ["health_interval", "1800"], ["health_active_interval", "60"],
  ["health_hysteresis", "3"], ["health_probe_url", "https://api.ipify.org?format=json"], ["failover_enabled", "1"],
  ["failover_cooldown", "120"], ["stats_enabled", "1"], ["stats_api_port", "10085"],
  ["traffic_sample_ms", "1000"], ["session_timeout_min", "0"], ["auto_backup_enabled", "0"],
];

/** Said before a re-applying write would start an xray the operator stopped while a node is still selected. */
export const STOPPED_XRAY_CLAUSE = "This will also start xray, which is currently stopped.";

/**
 * The reset names every section it reaches, because two of its keys belong to Subscriptions, one to Gateway ›
 * Network, and — owner decision 8, fix round 1 — the last two (`session_timeout_min`, `auto_backup_enabled`,
 * `routes.py` `_SETTINGS_RESET_KEYS`) belong to the sibling System screens Access and Backups; `_reapply_or_502`
 * runs unconditionally, so it always rebuilds the live tunnel. The two added clauses sit last, in the same
 * order the route resets them.
 */
export function resetConfirm(startsTunnel: boolean): string {
  const text =
    "Reset panel settings to their defaults? This also turns subscription auto-switch and tunnelled subscription " +
    "fetch back on, turns gateway DNS over DoH off, turns the idle timeout off, turns daily auto-backup off, and " +
    "rebuilds the live tunnel — devices may drop briefly. Nodes, subscriptions, anti-DPI profiles and routing " +
    "rules are kept.";
  return startsTunnel ? `${text}\n\n${STOPPED_XRAY_CLAUSE}` : text;
}

export function resetMessage(active: boolean): string {
  return active ? "settings reset to defaults · applied to the live tunnel" : "settings reset to defaults — applies on next Connect";
}

export const DANGER_ZONE_HINT =
  "Nodes, subscriptions, anti-DPI profiles and routing rules are kept. The reset reaches Subscriptions and Gateway too, and it rebuilds the live tunnel.";
