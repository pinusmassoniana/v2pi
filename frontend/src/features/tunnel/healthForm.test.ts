import { describe, expect, it } from "vitest";
import { SETTINGS } from "../../test/fixtures";
import {
  ACTIVE_INTERVAL_MESSAGE, COOLDOWN_MESSAGE, HYSTERESIS_MESSAGE, PROBE_URL_MESSAGE, SWEEP_MINUTES_MESSAGE, healthFormSchema, healthFormToPatch,
  settingsToHealthForm, sweepMinutes, sweepSeconds, type HealthFormValues,
} from "./healthForm";

const initial = settingsToHealthForm(SETTINGS);

function errorsOf(patch: Partial<HealthFormValues>): Record<string, string> {
  const result = healthFormSchema.safeParse({ ...initial, ...patch });
  return result.success ? {} : Object.fromEntries(result.error.issues.map((issue) => [issue.path.join("."), issue.message]));
}

describe("minutes and seconds", () => {
  it("the sweep reads in whole minutes, at least 1, and is stored as max(60, minutes × 60)", () => {
    expect(sweepMinutes(1800)).toBe(30);
    expect(sweepMinutes(600)).toBe(10);
    expect(sweepMinutes(90)).toBe(2);
    expect(sweepMinutes(20)).toBe(1);
    expect(sweepSeconds(30)).toBe(1800);
    expect(sweepSeconds(1)).toBe(60);
    expect(sweepSeconds(0)).toBe(60);
  });

  it("settings become the form's values", () => {
    expect(initial).toEqual({
      health_enabled: true, health_sweep_enabled: true, sweep_minutes: "10", active_seconds: "30",
      health_probe_url: "https://www.gstatic.com/generate_204", failover_enabled: true, hysteresis: "3", cooldown_seconds: "300",
    });
  });
});

describe("floors (G6)", () => {
  it("the stored values pass", () => {
    expect(errorsOf({})).toEqual({});
  });

  it("sweep ≥ 1 min, active check ≥ 10 s, hysteresis ≥ 1, cooldown ≥ 0 — each at its bound", () => {
    expect(errorsOf({ sweep_minutes: "1", active_seconds: "10", hysteresis: "1", cooldown_seconds: "0" })).toEqual({});
    expect(errorsOf({ sweep_minutes: "0", active_seconds: "9", hysteresis: "0", cooldown_seconds: "-1" })).toEqual({
      sweep_minutes: SWEEP_MINUTES_MESSAGE, active_seconds: ACTIVE_INTERVAL_MESSAGE, hysteresis: HYSTERESIS_MESSAGE, cooldown_seconds: COOLDOWN_MESSAGE,
    });
    expect(SWEEP_MINUTES_MESSAGE).toBe("Server sweep interval must be ≥ 1 min");
    expect(ACTIVE_INTERVAL_MESSAGE).toBe("Active-server check interval must be ≥ 10 s");
    expect(HYSTERESIS_MESSAGE).toBe("Hysteresis must be ≥ 1");
    expect(COOLDOWN_MESSAGE).toBe("Cooldown must be ≥ 0");
  });

  it("a blank, fractional or non-numeric entry is refused with its field's floor message", () => {
    expect(errorsOf({ sweep_minutes: "", active_seconds: "12.5", hysteresis: "three", cooldown_seconds: " " })).toEqual({
      sweep_minutes: SWEEP_MINUTES_MESSAGE, active_seconds: ACTIVE_INTERVAL_MESSAGE, hysteresis: HYSTERESIS_MESSAGE, cooldown_seconds: COOLDOWN_MESSAGE,
    });
  });

  it("the probe URL is an http(s) URL of at most 2048 characters", () => {
    expect(errorsOf({ health_probe_url: "https://api.ipify.org?format=json" })).toEqual({});
    expect(errorsOf({ health_probe_url: "http://probe.example/ok" })).toEqual({});
    expect(errorsOf({ health_probe_url: `https://a.example/${"x".repeat(2030)}` })).toEqual({});
    expect(errorsOf({ health_probe_url: `https://a.example/${"x".repeat(2031)}` })).toEqual({ health_probe_url: PROBE_URL_MESSAGE });
    for (const bad of ["", "api.ipify.org", "ftp://a.example", "https://"]) expect(errorsOf({ health_probe_url: bad })).toEqual({ health_probe_url: PROBE_URL_MESSAGE });
  });
});

describe("healthFormToPatch", () => {
  it("nothing changed sends nothing", () => {
    expect(healthFormToPatch(initial, initial)).toEqual({});
  });

  it("sends only the settings whose field changed, in stored units", () => {
    expect(healthFormToPatch({ ...initial, active_seconds: "60" }, initial)).toEqual({ health_active_interval: 60 });
    expect(healthFormToPatch({ ...initial, sweep_minutes: "30", hysteresis: "4" }, initial)).toEqual({ health_interval: 1800, health_hysteresis: 4 });
    expect(healthFormToPatch({ ...initial, health_enabled: false, failover_enabled: false }, initial)).toEqual({ health_enabled: false, failover_enabled: false });
    expect(healthFormToPatch({ ...initial, health_sweep_enabled: false, cooldown_seconds: "0", health_probe_url: " https://p.example/ " }, initial)).toEqual({
      health_sweep_enabled: false, failover_cooldown: 0, health_probe_url: "https://p.example/",
    });
  });

  it("a sweep stored in seconds that are not whole minutes is not re-sent unless its field was edited", () => {
    const odd = settingsToHealthForm({ ...SETTINGS, health_interval: 90 });
    expect(odd.sweep_minutes).toBe("2");
    expect(healthFormToPatch(odd, odd)).toEqual({});
    expect(healthFormToPatch({ ...odd, sweep_minutes: "3" }, odd)).toEqual({ health_interval: 180 });
  });

  it("a value typed back to what it was is not a change", () => {
    expect(healthFormToPatch({ ...initial, hysteresis: "3 " }, initial)).toEqual({});
  });
});
