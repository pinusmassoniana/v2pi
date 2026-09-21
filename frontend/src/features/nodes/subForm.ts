// The add / edit subscription form and the words of the Subscriptions screen: limits, the injected headers and
// query parameters, quota and refresh messages. Pure and unit-tested.
import { z } from "zod";
import type { RefreshAllResult, RefreshResult, SkippedEntries, Subscription, SubscriptionIn } from "../../api/client";
import { compactBytes } from "../../lib/format";
import { NO_PROFILE, profileFromValue, profileValue } from "../../lib/profiles";

/** backend _MAX_FIELD / _MAX_URL */
export const MAX_NAME = 512;
export const MAX_URL = 2048;
export const MINUTES_MESSAGE = "minutes must be a whole number, 0 or more";

// Re-exported: subscriptions, devices and usage all word bytes the same way, and the helper
// itself lives in lib/format so a card cannot pull this module (and zod with it) into the
// shell chunk just to format a number.
export { compactBytes };

export interface KeyValue { key: string; value: string }

export interface SubFormValues {
  name: string;
  url: string;
  /** Auto-update interval in minutes as typed; "0" is off. */
  minutes: string;
  enabled: boolean;
  /** The select's value: NO_PROFILE is the global default. */
  default_profile_id: string;
  headers: KeyValue[];
  queries: KeyValue[];
}

/** U7: a new subscription sends coarse OS data and the panel's user agent; the backend's x-hwid is left out on purpose. */
export const DEFAULT_HEADERS: readonly KeyValue[] = [
  { key: "x-device-os", value: "{device_os}" },
  { key: "user-agent", value: "v2pi/1.0" },
];

/**
 * A client the subscription fetch can pose as: the headers it sends, and how much of that is known.
 *
 * A provider can answer each client differently (a Remnawave panel picks the body by User-Agent) and
 * count devices by `x-hwid`, so "the gateway" and "a Happ install" can get different feeds. Every row
 * stays editable after a preset fills it. `{hwid16}` is a per-subscription pseudonym shaped like
 * Happ's own ID — stable across refreshes, unrelated between feeds, never the host's.
 *
 * Sources and what was verified: docs/2026-09-21-client-presets-research.md (local).
 */
export interface ClientPreset {
  id: string;
  label: string;
  headers: readonly KeyValue[];
  /** Said under the select: where the values come from, and which are plausible fill-ins. */
  note: string;
}

const HAPP_NOTE = "Header names as Happ sends them (Remnawave's HWID standard, and a captured Android request).";

export const CLIENT_PRESETS: readonly ClientPreset[] = [
  {
    id: "v2pi", label: "v2pi (default)", headers: DEFAULT_HEADERS,
    note: "Coarse OS data and the panel's own user agent. No device ID is sent.",
  },
  {
    id: "happ-android", label: "Happ · Android",
    headers: [
      { key: "user-agent", value: "Happ/4.3.0" },
      { key: "x-hwid", value: "{hwid16}" },
      { key: "x-device-os", value: "Android" },
      { key: "x-ver-os", value: "15" },
      { key: "x-device-model", value: "SM-S921B" },
      { key: "x-device-locale", value: "ru" },
      { key: "accept-encoding", value: "gzip" },
      { key: "connection", value: "close" },
    ],
    note: `${HAPP_NOTE} This set matches a real Android request; the version, OS version and model are plausible values.`,
  },
  {
    id: "happ-ios", label: "Happ · iOS",
    headers: [
      { key: "user-agent", value: "Happ/4.6.0" },
      { key: "x-hwid", value: "{hwid16}" },
      { key: "x-device-os", value: "iOS" },
      { key: "x-ver-os", value: "26.0" },
      { key: "x-device-model", value: "iPhone 15 Pro" },
      { key: "x-device-locale", value: "ru" },
      { key: "accept-encoding", value: "gzip" },
    ],
    note: `${HAPP_NOTE} The iOS user agent is not documented anywhere: it assumes the same "Happ/<version>" as Android.`,
  },
  {
    id: "happ-windows", label: "Happ · Windows",
    headers: [
      { key: "user-agent", value: "Happ/4.2.1" },
      { key: "x-hwid", value: "{hwid16}" },
      { key: "x-device-os", value: "Windows" },
      { key: "x-ver-os", value: "11" },
      { key: "x-device-model", value: "PC" },
      { key: "x-device-locale", value: "ru" },
      { key: "accept-encoding", value: "gzip" },
    ],
    note: `${HAPP_NOTE} Desktop values are not documented: the user agent, model and ID format are plausible guesses.`,
  },
  {
    id: "happ-macos", label: "Happ · macOS",
    headers: [
      { key: "user-agent", value: "Happ/4.6.0" },
      { key: "x-hwid", value: "{hwid16}" },
      { key: "x-device-os", value: "macOS" },
      { key: "x-ver-os", value: "26.0" },
      { key: "x-device-model", value: "Mac" },
      { key: "x-device-locale", value: "ru" },
      { key: "accept-encoding", value: "gzip" },
    ],
    note: `${HAPP_NOTE} On a Mac Happ is the App Store app; the user agent, model and ID format are plausible guesses.`,
  },
  {
    id: "v2raytun", label: "v2RayTun · Android",
    headers: [
      { key: "user-agent", value: "v2raytun/android" },
      { key: "x-hwid", value: "{hwid16}" },
      { key: "x-device-os", value: "Android" },
      { key: "x-ver-os", value: "15" },
      { key: "x-device-model", value: "SM-S921B" },
      { key: "x-app-version", value: "5.25.80" },
      { key: "accept-encoding", value: "gzip" },
    ],
    note: "Header names from v2RayTun's docs, the user agent from a public UA database, the app version from its store listing. The model and ID format are plausible guesses.",
  },
  {
    id: "v2rayng", label: "v2rayNG",
    headers: [
      { key: "user-agent", value: "v2rayNG/2.2.6" },
      { key: "connection", value: "close" },
      { key: "accept-encoding", value: "gzip" },
    ],
    note: "Taken from v2rayNG's own source (latest stable release). It sends no device ID.",
  },
  {
    id: "hiddify", label: "Hiddify",
    headers: [
      { key: "user-agent", value: "HiddifyNext/4.1.1 (android) like ClashMeta v2ray sing-box" },
      { key: "accept-encoding", value: "gzip" },
    ],
    note: "Taken from Hiddify's own source (latest release). It sends no device ID.",
  },
];

export const CUSTOM_PRESET = "custom";

/** Which preset these rows are, ignoring header-name case and order — or CUSTOM_PRESET once edited. */
export function matchPreset(headers: readonly KeyValue[]): string {
  const key = (list: readonly KeyValue[]) => list
    .filter((row) => row.key.trim())
    .map((row) => `${row.key.trim().toLowerCase()}\u0000${row.value}`)
    .sort()
    .join("\n");
  const current = key(headers);
  return CLIENT_PRESETS.find((preset) => key(preset.headers) === current)?.id ?? CUSTOM_PRESET;
}

/** The rows a preset fills in — fresh copies, so editing one never edits the preset. */
export function presetHeaders(id: string): KeyValue[] {
  return (CLIENT_PRESETS.find((preset) => preset.id === id)?.headers ?? []).map((row) => ({ ...row }));
}

/** Whether moving from `before` to `after` changes the device ID a provider would count. */
export function changesDeviceId(before: readonly KeyValue[], after: readonly KeyValue[]): boolean {
  const hwid = (list: readonly KeyValue[]) => list.find((row) => row.key.trim().toLowerCase() === "x-hwid")?.value ?? "";
  return hwid(before) !== hwid(after);
}

export const DEVICE_CHANGE_WARNING =
  "This changes the device ID the provider sees. A provider that limits devices counts it as a new one — "
  + "it can use up a slot, or push one of your other devices out.";

const rows = z.array(z.object({ key: z.string(), value: z.string() }));

export const subFormSchema = z.object({
  name: z.string().trim().min(1, "name is required").max(MAX_NAME, `name is at most ${MAX_NAME} characters`),
  url: z.string().trim().min(1, "URL is required").max(MAX_URL, `URL is at most ${MAX_URL} characters`),
  minutes: z.string().trim().regex(/^\d+$/, MINUTES_MESSAGE),
  enabled: z.boolean(),
  default_profile_id: z.string(),
  headers: rows,
  queries: rows,
}) satisfies z.ZodType<SubFormValues, SubFormValues>;

export function blankSubForm(): SubFormValues {
  return { name: "", url: "", minutes: "0", enabled: true, default_profile_id: NO_PROFILE, headers: DEFAULT_HEADERS.map((row) => ({ ...row })), queries: [] };
}

/** U5: minutes to the interval the backend stores — 0 is off; the backend raises anything else to at least 60 s. */
export function minutesToSeconds(minutes: number): number {
  return Math.max(0, Math.round(minutes)) * 60;
}

export function secondsToMinutes(seconds: number): number {
  return Math.round(Math.max(0, seconds) / 60);
}

/** The injection a subscription sends: rows with an empty name are skipped; a later row with the same name wins. */
export function buildInjection(headers: readonly KeyValue[], queries: readonly KeyValue[]): { headers: Record<string, string>; query: Record<string, string> } {
  const collect = (list: readonly KeyValue[]) => {
    const out: Record<string, string> = {};
    for (const row of list) {
      const key = row.key.trim();
      if (key) out[key] = row.value;
    }
    return out;
  };
  return { headers: collect(headers), query: collect(queries) };
}

function toRows(value: unknown): KeyValue[] {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  return Object.entries(value as Record<string, unknown>).map(([key, raw]) => ({ key, value: String(raw) }));
}

/** A stored injection as editable rows; anything malformed reads as no rows. */
export function injectionToRows(injection: Subscription["injection"] | null | undefined): { headers: KeyValue[]; queries: KeyValue[] } {
  return { headers: toRows(injection?.headers), queries: toRows(injection?.query) };
}

export function subToForm(sub: Subscription): SubFormValues {
  return {
    name: sub.name, url: sub.url, minutes: String(secondsToMinutes(sub.interval_sec)), enabled: sub.enabled,
    default_profile_id: profileValue(sub.default_profile_id),
    ...injectionToRows(sub.injection),
  };
}

/** U7 add sends name, URL, interval and injection; U5 edit also sends Enabled and the default profile for new nodes. */
export function formToSubIn(values: SubFormValues, edit: boolean): SubscriptionIn {
  const base: SubscriptionIn = {
    name: values.name.trim(),
    url: values.url.trim(),
    interval_sec: minutesToSeconds(Number(values.minutes)),
    injection: buildInjection(values.headers, values.queries),
  };
  if (!edit) return base;
  return { ...base, enabled: values.enabled, default_profile_id: profileFromValue(values.default_profile_id) };
}

/** U1: used (up + down) of total, only when the provider reports a total; 0–1, capped. */
export function quotaFraction(sub: Pick<Subscription, "up_bytes" | "down_bytes" | "total_bytes">): number | null {
  if (sub.total_bytes === null || sub.total_bytes <= 0) return null;
  return Math.min(1, ((sub.up_bytes ?? 0) + (sub.down_bytes ?? 0)) / sub.total_bytes);
}

/** U1: "20 GB / 100 GB · exp 2023-11-16" — the quota only with a total, the expiry date (UTC) whenever set. */
export function quotaText(sub: Pick<Subscription, "up_bytes" | "down_bytes" | "total_bytes" | "expire_at">): string {
  const parts: string[] = [];
  if (sub.total_bytes !== null) parts.push(`${compactBytes((sub.up_bytes ?? 0) + (sub.down_bytes ?? 0))} / ${compactBytes(sub.total_bytes)}`);
  if (sub.expire_at) parts.push(`exp ${new Date(sub.expire_at * 1000).toISOString().slice(0, 10)}`);
  return parts.join(" · ");
}

/** U1 auto-update. */
export function intervalText(intervalSec: number): string {
  return intervalSec > 0 ? `every ${secondsToMinutes(intervalSec)} min` : "off";
}

/** A6: how the backend's fixed skip labels are written for a person. An unknown key (a newer
 *  backend than this bundle) is shown as it arrived rather than dropped — the count is the point. */
const SKIPPED_NAMES: Record<string, string> = {
  vmess: "VMess", trojan: "Trojan", ss: "Shadowsocks", hysteria2: "Hysteria2", tuic: "TUIC",
  wireguard: "WireGuard", socks: "SOCKS", http: "HTTP", snell: "Snell", anytls: "AnyTLS",
  juicity: "Juicity", mieru: "Mieru", invalid: "unusable", other: "other",
};

export function skippedTotal(skipped: SkippedEntries | null | undefined): number {
  return Object.values(skipped ?? {}).reduce((sum, count) => sum + (count > 0 ? count : 0), 0);
}

/** "Trojan ×3 · Hysteria2 ×2 · unusable ×1", biggest first, so the reason reads before the tail. */
export function skippedText(skipped: SkippedEntries | null | undefined): string {
  return Object.entries(skipped ?? {})
    .filter(([, count]) => count > 0)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([label, count]) => `${SKIPPED_NAMES[label] ?? label} ×${count}`)
    .join(" · ");
}

/** N4 import: "imported 2/3 node(s) (clash) · 4 unsupported (Trojan ×4)". */
export function importedMessage(result: { added: number; total: number; format: string; skipped?: SkippedEntries }): string {
  const unsupported = skippedTotal(result.skipped);
  const tail = unsupported > 0 ? ` · ${unsupported} unsupported (${skippedText(result.skipped)})` : "";
  return `imported ${result.added}/${result.total} node(s) (${result.format})${tail}`;
}

export interface Outcome { text: string; error: boolean }

/** U2: refreshSub never throws on a fetch or parse failure — it answers ok: false with a status or error. */
export function refreshOneMessage(name: string, result: RefreshResult | null | undefined): Outcome {
  const error = Boolean(result?.error) || result?.ok === false;
  return { text: `${name}: ${result?.status ?? result?.error ?? "refreshed"}`, error };
}

/** U3: "1/2 refreshed · 1 failed — home: fetch failed: timeout", naming at most three failures. */
export function refreshAllMessage(result: RefreshAllResult): Outcome {
  const results = Array.isArray(result.results) ? result.results : Object.values(result.results ?? {});
  const failures = results.filter((item) => item.error || item.ok === false);
  const details = failures.slice(0, 3).map((item) => `${item.name ?? `#${item.id ?? "?"}`}: ${item.error ?? item.status ?? "failed"}`).join(" · ");
  const failed = result.failed > 0 ? ` · ${result.failed} failed${details ? ` — ${details}` : ""}` : "";
  return { text: `${result.succeeded}/${result.attempted} refreshed${failed}`, error: result.failed > 0 };
}

/** U6: the confirmation, word for word — delete_subscription detaches the nodes before dropping the row (nodes/store.py). */
export function deleteSubMessage(sub: Pick<Subscription, "name" | "node_count">): string {
  return `Delete subscription "${sub.name}"? Its ${sub.node_count} node(s) are detached to Servers (an active connection is kept).`;
}
