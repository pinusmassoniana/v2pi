// System › Panel, the traffic-stats card: the three settings it edits, their floors in the gateway's own words, the
// patch a save sends, and what the collector's health says. Pure and unit-tested.
import { z } from "zod";
import type { Diagnostics, Settings } from "../../api/client";
import { SETTINGS_REAPPLY_KEYS } from "../../api/client";

/** The gateway reserves its tproxy port and its local proxy port (config.py), and refuses either here. */
export const RESERVED_PORTS = [52345, 10808] as const;

// Every message below is `config.py: validate_setting_values`' own, word for word — the card must not invent a
// different sentence for the same refusal, or a client check and a server check read as two different problems.
export const PORT_RANGE_MESSAGE = "stats_api_port must be 1..65535";
export const PORT_RESERVED_MESSAGE = "stats_api_port collides with a system port";
export const SAMPLE_MIN_MESSAGE = "traffic_sample_ms must be >= 500";
export const SAMPLE_MAX_MESSAGE = "traffic_sample_ms must be <= 60000";
export const integerMessage = (key: string) => `${key} must be an integer`;

export type StatsSettingKey = "stats_enabled" | "stats_api_port" | "traffic_sample_ms";

export interface StatsFormValues {
  stats_enabled: boolean;
  stats_api_port: string;
  traffic_sample_ms: string;
}

function wholeNumber(value: string): number | null {
  return /^\d+$/.test(value.trim()) ? Number(value.trim()) : null;
}

export const statsFormSchema = z.object({
  stats_enabled: z.boolean(),
  stats_api_port: z.string().superRefine((value, ctx) => {
    const port = wholeNumber(value);
    if (port === null) return void ctx.addIssue({ code: "custom", message: integerMessage("stats_api_port") });
    if (port < 1 || port > 65535) return void ctx.addIssue({ code: "custom", message: PORT_RANGE_MESSAGE });
    if ((RESERVED_PORTS as readonly number[]).includes(port)) ctx.addIssue({ code: "custom", message: PORT_RESERVED_MESSAGE });
  }),
  traffic_sample_ms: z.string().superRefine((value, ctx) => {
    const ms = wholeNumber(value);
    if (ms === null) return void ctx.addIssue({ code: "custom", message: integerMessage("traffic_sample_ms") });
    if (ms < 500) return void ctx.addIssue({ code: "custom", message: SAMPLE_MIN_MESSAGE });
    if (ms > 60_000) ctx.addIssue({ code: "custom", message: SAMPLE_MAX_MESSAGE });
  }),
}) satisfies z.ZodType<StatsFormValues, StatsFormValues>;

export function settingsToStatsForm(settings: Settings): StatsFormValues {
  return {
    stats_enabled: settings.stats_enabled,
    stats_api_port: String(settings.stats_api_port),
    traffic_sample_ms: String(settings.traffic_sample_ms),
  };
}

/**
 * ONLY the fields that changed. This is what keeps a sample-interval-only save a plain settings write:
 * `settingsWriteKey` classifies by the keys present, and `api.putSettings` picks its timeout by the same test — so
 * sending all three every time would rebuild the live tunnel for an interval nobody asked to re-apply.
 */
export function statsPatch(values: StatsFormValues, initial: StatsFormValues): Partial<Pick<Settings, StatsSettingKey>> {
  const patch: Partial<Pick<Settings, StatsSettingKey>> = {};
  if (values.stats_enabled !== initial.stats_enabled) patch.stats_enabled = values.stats_enabled;
  if (values.stats_api_port.trim() !== initial.stats_api_port) patch.stats_api_port = Number(values.stats_api_port);
  if (values.traffic_sample_ms.trim() !== initial.traffic_sample_ms) patch.traffic_sample_ms = Number(values.traffic_sample_ms);
  return patch;
}

/** Whether this save waits on a rebuild at all: only the keys baked into the xray config do (routes.py _SETTINGS_CONFIG_KEYS, shared here as SETTINGS_REAPPLY_KEYS). */
export function statsPatchReapplies(patch: Partial<Pick<Settings, StatsSettingKey>>): boolean {
  return SETTINGS_REAPPLY_KEYS.some((key) => key in patch);
}

/**
 * What the save says. Two things must both hold for a live-apply claim: a node is active, AND the patch is one
 * `_reapply_or_502` actually runs for (routes.py:1449-1450 only reapplies when the patch intersects
 * `_SETTINGS_CONFIG_KEYS`) — `traffic_sample_ms` alone never rebuilds anything, so an interval-only save with a
 * node connected must still read as "applies on next Connect", not as a live apply that did not happen.
 */
export function statsSavedMessage(patch: Partial<Pick<Settings, StatsSettingKey>>, active: boolean): string {
  if (!active || !statsPatchReapplies(patch)) return "saved — applies on next Connect";
  if ("stats_enabled" in patch) return `traffic stats ${patch.stats_enabled ? "on" : "off"} · applied to the live tunnel`;
  return "saved · applied to the live tunnel";
}

/**
 * The warning that belongs on THIS card and nowhere else: the panel has never reached xray's StatsService and has
 * tried. It is the only place an operator can learn why the Home graph is flat. A collector that has succeeded at
 * least once is not warned about, however stale — a momentary failure is not a misconfiguration.
 */
export function collectorWarning(diagnostics: Diagnostics | undefined, port: string): { title: string; text: string } | null {
  if (!diagnostics || diagnostics.stats_last_ok_at !== null || diagnostics.stats_fail_count <= 0) return null;
  return {
    title: `The panel cannot reach xray's stats API on port ${port}`,
    text: `${diagnostics.stats_error || "no reason reported"}. This is why the Home graph is flat; the durable totals already recorded are kept.`,
  };
}

/**
 * The healthy readout beside the fields, when the collector has succeeded at least once. `stats_fail_count` resets
 * on every success and on reconfigure (stats/client.py:58-70), so it is *consecutive* failures since the last
 * success — not since the collector started — and the copy says exactly that.
 */
export function collectorOkLabel(diagnostics: Diagnostics | undefined): string | null {
  if (!diagnostics || diagnostics.stats_last_ok_at === null) return null;
  return `${diagnostics.stats_fail_count} consecutive failures since the last success`;
}

/**
 * What the result line says while the form holds invalid values: "N field(s) invalid · fix them to save".
 * Hoisted (fix round 1) so the phone layout's footer — which copies this expression verbatim — pluralises the
 * same way instead of duplicating a "1 fields invalid" bug.
 */
export function statsInvalidMessage(errorCount: number): string {
  return `${errorCount} field${errorCount === 1 ? "" : "s"} invalid · fix them to save`;
}

export const STATS_NOTE =
  "The panel reads xray's StatsService on this port to draw the Home graph. Turning collection off stops the graph " +
  "and the data-used counters; the durable totals already recorded are kept.";
