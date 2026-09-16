import { describe, expect, it } from "vitest";
import { SETTINGS_CONNECTION_WRITE, SETTINGS_WRITE, settingsWriteKey } from "../../api/invalidation";
import { DIAGNOSTICS, SETTINGS } from "../../test/fixtures";
import {
  EXPORT_EXCLUDED, MAX_SETTINGS_BYTES, RESET_KEYS, SETTINGS_KEYS, STOPPED_XRAY_CLAUSE, exportSettings, exportedText,
  importChecks, resetConfirm, resetMessage, settingValueIssue, settingsFileTooLarge,
} from "./settingsFile";
import {
  PORT_RANGE_MESSAGE, PORT_RESERVED_MESSAGE, RESERVED_PORTS, SAMPLE_MAX_MESSAGE, SAMPLE_MIN_MESSAGE, collectorOkLabel,
  collectorWarning, settingsToStatsForm, statsFormSchema, statsPatch, statsPatchReapplies, statsSavedMessage,
} from "./statsForm";

const BASE = settingsToStatsForm(SETTINGS);

function messages(values: Partial<typeof BASE>): string[] {
  const result = statsFormSchema.safeParse({ ...BASE, ...values });
  return result.success ? [] : result.error.issues.map((issue) => issue.message);
}

describe("statsForm rules", () => {
  it("uses the gateway's own sentences, so a client refusal and a server refusal read the same", () => {
    expect(messages({ stats_api_port: "0" })).toEqual([PORT_RANGE_MESSAGE]);
    expect(messages({ stats_api_port: "65536" })).toEqual([PORT_RANGE_MESSAGE]);
    expect(messages({ stats_api_port: "52345" })).toEqual([PORT_RESERVED_MESSAGE]);
    expect(messages({ stats_api_port: "10808" })).toEqual([PORT_RESERVED_MESSAGE]);
    expect(RESERVED_PORTS).toEqual([52345, 10808]);
    expect(messages({ traffic_sample_ms: "250" })).toEqual([SAMPLE_MIN_MESSAGE]);
    expect(messages({ traffic_sample_ms: "60001" })).toEqual([SAMPLE_MAX_MESSAGE]);
    expect(messages({ traffic_sample_ms: "500" })).toEqual([]);
    expect(messages({ traffic_sample_ms: "60000" })).toEqual([]);
    expect(messages({ stats_api_port: "1" })).toEqual([]);
    expect(messages({ stats_api_port: "65535" })).toEqual([]);
  });

  it("a value that is not a whole number is not an integer, in the gateway's words", () => {
    for (const bad of ["", " ", "abc", "10.5", "-1", "1e3"]) {
      expect(messages({ stats_api_port: bad })).toEqual(["stats_api_port must be an integer"]);
      expect(messages({ traffic_sample_ms: bad })).toEqual(["traffic_sample_ms must be an integer"]);
    }
    // a boolean is explicitly not an integer on the backend either
    expect(settingValueIssue("traffic_sample_ms", true)).toBe("traffic_sample_ms must be an integer");
    expect(settingValueIssue("stats_api_port", false)).toBe("stats_api_port must be an integer");
  });
});

describe("statsPatch", () => {
  it("sends only what changed, which is what keeps an interval-only save a plain settings write", () => {
    expect(statsPatch(BASE, BASE)).toEqual({});
    const interval = statsPatch({ ...BASE, traffic_sample_ms: "2000" }, BASE);
    expect(interval).toEqual({ traffic_sample_ms: 2000 });
    expect(statsPatchReapplies(interval)).toBe(false);
    expect(settingsWriteKey(interval)).toBe(SETTINGS_WRITE);

    const port = statsPatch({ ...BASE, stats_api_port: "10086" }, BASE);
    expect(port).toEqual({ stats_api_port: 10086 });
    expect(statsPatchReapplies(port)).toBe(true);
    expect(settingsWriteKey(port)).toBe(SETTINGS_CONNECTION_WRITE);

    const off = statsPatch({ ...BASE, stats_enabled: !BASE.stats_enabled }, BASE);
    expect(off).toEqual({ stats_enabled: !SETTINGS.stats_enabled });
    expect(settingsWriteKey(off)).toBe(SETTINGS_CONNECTION_WRITE);

    // all three at once is still one patch, and still a connection write
    const all = statsPatch({ stats_enabled: !BASE.stats_enabled, stats_api_port: "10086", traffic_sample_ms: "2000" }, BASE);
    expect(Object.keys(all).sort()).toEqual(["stats_api_port", "stats_enabled", "traffic_sample_ms"]);
    expect(settingsWriteKey(all)).toBe(SETTINGS_CONNECTION_WRITE);
  });

  it("says what happened, and never claims a live apply the gateway did not do", () => {
    expect(statsSavedMessage({ stats_enabled: true }, true)).toBe("traffic stats on · applied to the live tunnel");
    expect(statsSavedMessage({ stats_enabled: false }, true)).toBe("traffic stats off · applied to the live tunnel");
    expect(statsSavedMessage({ stats_api_port: 10086 }, true)).toBe("saved · applied to the live tunnel");
    for (const patch of [{ stats_enabled: true }, { traffic_sample_ms: 2000 }, {}]) {
      expect(statsSavedMessage(patch, false)).toBe("saved — applies on next Connect");
    }
    // Review fix round 1: traffic_sample_ms is not in _SETTINGS_CONFIG_KEYS (routes.py:1417), so put_settings
    // never runs _reapply_or_502 for it — a node being active does not make an interval-only save a live apply.
    expect(statsSavedMessage({ traffic_sample_ms: 2000 }, true)).toBe("saved — applies on next Connect");
  });
});

describe("collector health", () => {
  it("warns only when the collector has never succeeded and has tried", () => {
    expect(collectorWarning(DIAGNOSTICS, "10085")).toBeNull();
    expect(collectorWarning(undefined, "10085")).toBeNull();
    // has failed, but has also succeeded before — a momentary failure is not a misconfiguration
    expect(collectorWarning({ ...DIAGNOSTICS, stats_fail_count: 12 }, "10085")).toBeNull();
    // never succeeded, never tried either: nothing to say yet
    expect(collectorWarning({ ...DIAGNOSTICS, stats_last_ok_at: null, stats_fail_count: 0 }, "10085")).toBeNull();

    const broken = collectorWarning({ ...DIAGNOSTICS, stats_last_ok_at: null, stats_fail_count: 42, stats_error: "connection refused" }, "10085");
    expect(broken).toEqual({
      title: "The panel cannot reach xray's stats API on port 10085",
      text: "connection refused. This is why the Home graph is flat; the durable totals already recorded are kept.",
    });
    expect(collectorWarning({ ...DIAGNOSTICS, stats_last_ok_at: null, stats_fail_count: 1, stats_error: "" }, "10085")?.text)
      .toContain("no reason reported");
  });

  it("a healthy collector reports its consecutive failure count instead", () => {
    // Task 2 review correction: fail_count resets on every success and on reconfigure (stats/client.py:58-70),
    // so the copy must read "since the last success", never "since start" — the collector may have been
    // running for days.
    expect(collectorOkLabel(DIAGNOSTICS)).toBe("0 consecutive failures since the last success");
    expect(collectorOkLabel({ ...DIAGNOSTICS, stats_last_ok_at: null })).toBeNull();
    expect(collectorOkLabel(undefined)).toBeNull();
  });
});

describe("exportSettings", () => {
  it("drops the routing-owned key and nothing else, and makes no request", () => {
    const exported = exportSettings(SETTINGS);
    expect(exported).not.toHaveProperty(EXPORT_EXCLUDED);
    expect(Object.keys(exported).sort()).toEqual(SETTINGS_KEYS.filter((key) => key !== EXPORT_EXCLUDED).sort());
    expect(Object.keys(exported)).toHaveLength(16);
    for (const key of Object.keys(exported)) expect(exported[key as keyof typeof exported]).toBe(SETTINGS[key as keyof typeof SETTINGS]);
    expect(JSON.parse(exportedText(SETTINGS))).toEqual(exported);
  });
});

describe("importChecks", () => {
  const good = JSON.stringify(exportSettings(SETTINGS));

  it("passes a file this panel exported, and names which keys would rebuild the tunnel", () => {
    const result = importChecks(good);
    expect(result.checks.map((check) => check.ok)).toEqual([true, true, true, true]);
    expect(result.checks[1]!.label).toBe("16 fields, all known settings keys");
    expect(result.checks[3]!.label).toBe("4 of them rebuild the live tunnel: tunneled_fetch, dns_intercept, stats_enabled, stats_api_port");
    expect(result.patch).not.toBeNull();
    expect(result.patch).not.toHaveProperty(EXPORT_EXCLUDED);
    expect(settingsWriteKey(result.patch!)).toBe(SETTINGS_CONNECTION_WRITE);
  });

  it("names the unknown fields rather than letting one stray key produce an opaque 422", () => {
    const result = importChecks(JSON.stringify({ stats_enabled: true, health_probe_urls: "x", rw_enabled: "1" }));
    expect(result.unknown).toEqual(["health_probe_urls", "rw_enabled"]);
    expect(result.checks[1]).toEqual({ ok: false, label: "2 fields are not settings keys: health_probe_urls, rw_enabled" });
    expect(result.patch).toBeNull();
    expect(importChecks(JSON.stringify({ nope: 1 })).checks[1]!.label).toBe("1 field is not a settings key: nope");
  });

  it("refuses anything that is not an object, and a file with nothing in it", () => {
    for (const text of ["[]", "null", '"a"', "{oops"]) {
      const result = importChecks(text);
      expect(result.checks).toHaveLength(1);
      expect(result.checks[0]).toEqual({ ok: false, label: "parses as JSON, and is an object" });
      expect(result.patch).toBeNull();
    }
    const empty = importChecks("{}");
    expect(empty.checks[1]).toEqual({ ok: false, label: "no settings in this file" });
    expect(empty.patch).toBeNull();
  });

  it("checks values with the cards' own rules, and refuses an explicit null the way the schema does", () => {
    expect(importChecks(JSON.stringify({ traffic_sample_ms: 250 })).checks[2]).toEqual({ ok: false, label: SAMPLE_MIN_MESSAGE });
    expect(importChecks(JSON.stringify({ stats_api_port: 52345 })).checks[2]!.label).toBe(PORT_RESERVED_MESSAGE);
    expect(importChecks(JSON.stringify({ health_interval: 30 })).checks[2]!.label).toBe("health_interval must be >= 60");
    expect(importChecks(JSON.stringify({ session_timeout_min: -1 })).checks[2]!.label).toBe("session_timeout_min must be >= 0");
    expect(importChecks(JSON.stringify({ session_timeout_min: "1.5" })).checks[2]!.label).toBe("session_timeout_min must be an integer");
    expect(importChecks(JSON.stringify({ stats_enabled: "yes" })).checks[2]!.label).toBe("stats_enabled must be true or false");
    expect(importChecks(JSON.stringify({ dns_intercept: null })).checks[2]!.label).toBe("dns_intercept may be omitted but not null");
    // a settings row's own shapes are accepted, because pydantic accepts them too
    for (const value of [true, false, 0, 1, "0", "1"]) expect(settingValueIssue("stats_enabled", value)).toBeNull();
    for (const value of [1000, "1000"]) expect(settingValueIssue("traffic_sample_ms", value)).toBeNull();
  });

  it("a partial file only carries what it holds, and is classified by that", () => {
    const plain = importChecks(JSON.stringify({ health_interval: 1800, session_timeout_min: 0 }));
    expect(plain.reapplyKeys).toEqual([]);
    expect(plain.checks[3]!.label).toBe("none of them rebuild the live tunnel");
    expect(settingsWriteKey(plain.patch!)).toBe(SETTINGS_WRITE);
    const reapply = importChecks(JSON.stringify({ health_interval: 1800, dns_intercept: true }));
    expect(reapply.reapplyKeys).toEqual(["dns_intercept"]);
    expect(settingsWriteKey(reapply.patch!)).toBe(SETTINGS_CONNECTION_WRITE);
    // routing_default_action is accepted in a file but never sent on
    const routing = importChecks(JSON.stringify({ routing_default_action: "direct", stats_enabled: true }));
    expect(routing.patch).toEqual({ stats_enabled: true });
  });

  it("an oversized file gets a sentence instead of a 413 raised before the session is read", () => {
    expect(MAX_SETTINGS_BYTES).toBe(1024 * 1024);
    expect(settingsFileTooLarge(MAX_SETTINGS_BYTES)).toBeNull();
    expect(settingsFileTooLarge(MAX_SETTINGS_BYTES + 1)?.title).toBe("That file is larger than 1 MB");
  });
});

describe("the reset", () => {
  it("is the sixteen keys the route writes, in its order, with their defaults", () => {
    expect(RESET_KEYS).toHaveLength(16);
    expect(RESET_KEYS.map(([key]) => key)).toEqual([
      "tunneled_fetch", "subs_auto_switch", "dns_intercept", "health_enabled", "health_sweep_enabled",
      "health_interval", "health_active_interval", "health_hysteresis", "health_probe_url", "failover_enabled",
      "failover_cooldown", "stats_enabled", "stats_api_port", "traffic_sample_ms", "session_timeout_min", "auto_backup_enabled",
    ]);
    // routing-owned keys are never touched by the route, so they are not listed here either
    expect(RESET_KEYS.map(([key]) => key)).not.toContain("routing_default_action");
    expect(Object.fromEntries(RESET_KEYS)).toMatchObject({ health_interval: "1800", stats_api_port: "10085", auto_backup_enabled: "0" });
  });

  it("names every section it reaches, and the rebuild it always does", () => {
    const text = resetConfirm(false);
    expect(text).toContain("turns subscription auto-switch and tunnelled subscription fetch back on");
    expect(text).toContain("turns gateway DNS over DoH off");
    expect(text).toContain("rebuilds the live tunnel");
    expect(text).toContain("Nodes, subscriptions, anti-DPI profiles and routing rules are kept.");
    expect(text).not.toContain(STOPPED_XRAY_CLAUSE);
    expect(resetConfirm(true).endsWith(`\n\n${STOPPED_XRAY_CLAUSE}`)).toBe(true);
  });

  it("says whether it reached the live tunnel", () => {
    expect(resetMessage(true)).toBe("settings reset to defaults · applied to the live tunnel");
    expect(resetMessage(false)).toBe("settings reset to defaults — applies on next Connect");
  });
});
