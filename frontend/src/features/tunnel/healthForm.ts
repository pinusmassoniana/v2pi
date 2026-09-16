// Tunnel › Health & failover: the health-check and auto-failover settings as the form edits them, their floors, and
// the partial settings patch a save sends. Pure and unit-tested.
import { z } from "zod";
import type { Settings } from "../../api/client";

/** backend SETTINGS_INT_BOUNDS floors and the client messages the Svelte settings screen used. */
export const SWEEP_MINUTES_MESSAGE = "Server sweep interval must be ≥ 1 min";
export const ACTIVE_INTERVAL_MESSAGE = "Active-server check interval must be ≥ 10 s";
export const HYSTERESIS_MESSAGE = "Hysteresis must be ≥ 1";
export const COOLDOWN_MESSAGE = "Cooldown must be ≥ 0";
export const PROBE_URL_MESSAGE = "Probe URL must be an http(s) URL of at most 2048 characters";
export const MAX_PROBE_URL = 2048;

export const SAVED_MESSAGE = "Health settings saved";
export const ACTIVE_CHECK_EXPLAINER =
  "A real request through the connected server, via a throwaway xray so your live connection is untouched. Auto-failover reads this check, not the sweep — so you can turn the sweep off and keep failover working.";
export const DEMOTION_NOTE = "A node that was just failed away from isn't picked again for 10 minutes.";
export const NO_REAPPLY_NOTE = "Health and failover changes don't re-apply the tunnel.";

/** The settings this screen edits. */
export type HealthSettingKey =
  | "health_enabled" | "health_sweep_enabled" | "health_interval" | "health_active_interval" | "health_probe_url"
  | "failover_enabled" | "health_hysteresis" | "failover_cooldown";

/** The form's values; numbers as typed. The sweep interval is edited in minutes, the rest in the units they are stored in. */
export interface HealthFormValues {
  health_enabled: boolean;
  health_sweep_enabled: boolean;
  sweep_minutes: string;
  active_seconds: string;
  health_probe_url: string;
  failover_enabled: boolean;
  hysteresis: string;
  cooldown_seconds: string;
}

/** A whole number of at least `floor`, as typed. */
function atLeast(floor: number, message: string) {
  return z.string().refine((value) => /^\d+$/.test(value.trim()) && Number(value) >= floor, message);
}

export const healthFormSchema = z.object({
  health_enabled: z.boolean(),
  health_sweep_enabled: z.boolean(),
  sweep_minutes: atLeast(1, SWEEP_MINUTES_MESSAGE),
  active_seconds: atLeast(10, ACTIVE_INTERVAL_MESSAGE),
  health_probe_url: z.string().trim().max(MAX_PROBE_URL, PROBE_URL_MESSAGE).regex(/^https?:\/\/\S+$/, PROBE_URL_MESSAGE),
  failover_enabled: z.boolean(),
  hysteresis: atLeast(1, HYSTERESIS_MESSAGE),
  cooldown_seconds: atLeast(0, COOLDOWN_MESSAGE),
}) satisfies z.ZodType<HealthFormValues, HealthFormValues>;

/** Seconds as whole minutes for the sweep field: at least 1. */
export function sweepMinutes(seconds: number): number {
  return Math.max(1, Math.round(seconds / 60));
}

/** Minutes back to the seconds the backend stores: at least 60. */
export function sweepSeconds(minutes: number): number {
  return Math.max(60, Math.round(minutes) * 60);
}

export function settingsToHealthForm(settings: Settings): HealthFormValues {
  return {
    health_enabled: settings.health_enabled,
    health_sweep_enabled: settings.health_sweep_enabled,
    sweep_minutes: String(sweepMinutes(settings.health_interval)),
    active_seconds: String(settings.health_active_interval),
    health_probe_url: settings.health_probe_url,
    failover_enabled: settings.failover_enabled,
    hysteresis: String(settings.health_hysteresis),
    cooldown_seconds: String(settings.failover_cooldown),
  };
}

/**
 * The partial `PUT /settings` a save sends: only the settings whose field differs from what the form started with, in
 * the units the backend stores. An untouched sweep field is never re-sent, even when its stored seconds are not a
 * whole number of minutes.
 */
export function healthFormToPatch(values: HealthFormValues, initial: HealthFormValues): Partial<Pick<Settings, HealthSettingKey>> {
  const patch: Partial<Pick<Settings, HealthSettingKey>> = {};
  if (values.health_enabled !== initial.health_enabled) patch.health_enabled = values.health_enabled;
  if (values.health_sweep_enabled !== initial.health_sweep_enabled) patch.health_sweep_enabled = values.health_sweep_enabled;
  if (values.sweep_minutes.trim() !== initial.sweep_minutes) patch.health_interval = sweepSeconds(Number(values.sweep_minutes));
  if (values.active_seconds.trim() !== initial.active_seconds) patch.health_active_interval = Number(values.active_seconds);
  if (values.health_probe_url.trim() !== initial.health_probe_url) patch.health_probe_url = values.health_probe_url.trim();
  if (values.failover_enabled !== initial.failover_enabled) patch.failover_enabled = values.failover_enabled;
  if (values.hysteresis.trim() !== initial.hysteresis) patch.health_hysteresis = Number(values.hysteresis);
  if (values.cooldown_seconds.trim() !== initial.cooldown_seconds) patch.failover_cooldown = Number(values.cooldown_seconds);
  return patch;
}
