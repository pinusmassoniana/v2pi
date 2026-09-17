import { describe, expect, it } from "vitest";
import { NOW_SEC, REFRESH_ALL, SUBS } from "../../test/fixtures";
import { DEFAULT_HEADERS, blankSubForm, buildInjection, compactBytes, deleteSubMessage, formToSubIn, importedMessage, injectionToRows, intervalText, minutesToSeconds, quotaFraction, quotaText, refreshAllMessage, refreshOneMessage, secondsToMinutes, skippedText, skippedTotal, subFormSchema, subToForm, type SubFormValues } from "./subForm";

const issues = (values: SubFormValues) => {
  const result = subFormSchema.safeParse(values);
  return result.success ? {} : Object.fromEntries(result.error.issues.map((issue) => [issue.path.join("."), issue.message]));
};

describe("subscription form", () => {
  it("name and URL are required; minutes are a whole number from 0", () => {
    const blank = blankSubForm();
    expect(issues(blank)).toEqual({ name: "name is required", url: "URL is required" });
    const filled = { ...blank, name: "big-feed", url: "https://feed.example.net/api/sub?token=a41c09" };
    expect(issues(filled)).toEqual({});
    for (const minutes of ["0", "60", " 5 "]) expect(issues({ ...filled, minutes })).toEqual({});
    for (const minutes of ["-1", "1.5", "", "hourly"]) expect(issues({ ...filled, minutes })).toEqual({ minutes: "minutes must be a whole number, 0 or more" });
    expect(issues({ ...filled, url: `https://x.example/${"a".repeat(2048)}` })).toEqual({ url: "URL is at most 2048 characters" });
  });

  it("a new subscription starts with the default headers, no query params, auto-update off and enabled", () => {
    const blank = blankSubForm();
    expect(blank).toEqual({
      name: "", url: "", minutes: "0", enabled: true, default_profile_id: "",
      headers: [{ key: "x-device-os", value: "{device_os}" }, { key: "user-agent", value: "v2pi/1.0" }], queries: [],
    });
    blank.headers[0]!.value = "changed";
    expect(DEFAULT_HEADERS[0]!.value).toBe("{device_os}");
    expect(blankSubForm().headers[0]!.value).toBe("{device_os}");
  });

  it("minutes to seconds and back: 0 is off, fractions round", () => {
    expect(minutesToSeconds(0)).toBe(0);
    expect(minutesToSeconds(60)).toBe(3_600);
    expect(minutesToSeconds(1.4)).toBe(60);
    expect(minutesToSeconds(-5)).toBe(0);
    expect(secondsToMinutes(21_600)).toBe(360);
    expect(secondsToMinutes(90)).toBe(2);
    expect(secondsToMinutes(-1)).toBe(0);
  });

  it("injection: rows to maps and back, skipping unnamed rows, a later duplicate winning", () => {
    const injection = buildInjection(
      [{ key: " x-device-os ", value: "{device_os}" }, { key: "", value: "dropped" }, { key: "user-agent", value: "a" }, { key: "user-agent", value: "v2pi/1.0" }],
      [{ key: "type", value: "vless" }],
    );
    expect(injection).toEqual({ headers: { "x-device-os": "{device_os}", "user-agent": "v2pi/1.0" }, query: { type: "vless" } });
    expect(injectionToRows(injection)).toEqual({
      headers: [{ key: "x-device-os", value: "{device_os}" }, { key: "user-agent", value: "v2pi/1.0" }], queries: [{ key: "type", value: "vless" }],
    });
    expect(injectionToRows({})).toEqual({ headers: [], queries: [] });
    expect(injectionToRows({ headers: ["not", "a", "map"], query: { n: 1 } })).toEqual({ headers: [], queries: [{ key: "n", value: "1" }] });
    expect(injectionToRows(null)).toEqual({ headers: [], queries: [] });
  });

  it("edit round-trip: the stored subscription opens as the form and saves back to the same values", () => {
    const form = subToForm(SUBS[0]!);
    expect(form).toEqual({
      name: "work", url: SUBS[0]!.url, minutes: "60", enabled: true, default_profile_id: "",
      headers: [{ key: "x-device-os", value: "{device_os}" }, { key: "user-agent", value: "v2pi/1.0" }], queries: [{ key: "type", value: "vless" }],
    });
    expect(formToSubIn(form, true)).toEqual({
      name: "work", url: SUBS[0]!.url, interval_sec: 3_600, injection: SUBS[0]!.injection, enabled: true, default_profile_id: null,
    });
    expect(subToForm(SUBS[1]!)).toMatchObject({ minutes: "360", default_profile_id: "2", headers: [], queries: [] });
    expect(formToSubIn(subToForm(SUBS[1]!), true)).toMatchObject({ default_profile_id: 2 });
  });

  it("add sends no Enabled and no default profile", () => {
    const values = { ...blankSubForm(), name: " big-feed ", url: " https://feed.example.net/sub ", minutes: "60" };
    expect(formToSubIn(values, false)).toEqual({
      name: "big-feed", url: "https://feed.example.net/sub", interval_sec: 3_600,
      injection: { headers: { "x-device-os": "{device_os}", "user-agent": "v2pi/1.0" }, query: {} },
    });
  });
});

describe("subscription words", () => {
  it("bytes in decimal units with at most one decimal", () => {
    expect(compactBytes(18_400_000_000)).toBe("18.4 GB");
    expect(compactBytes(100e9)).toBe("100 GB");
    expect(compactBytes(512e6)).toBe("512 MB");
    expect(compactBytes(2.5e12)).toBe("2.5 TB");
    expect(compactBytes(999)).toBe("999 B");
  });

  it("quota: used of total only with a total, and the expiry date whenever set", () => {
    expect(quotaText(SUBS[0]!)).toBe(`20 GB / 100 GB · exp ${new Date((NOW_SEC + 2 * 86_400) * 1000).toISOString().slice(0, 10)}`);
    expect(quotaText(SUBS[1]!)).toBe("86 GB / 100 GB");
    expect(quotaText(SUBS[2]!)).toBe(`exp ${new Date((NOW_SEC - 86_400) * 1000).toISOString().slice(0, 10)}`);
    expect(quotaText({ up_bytes: null, down_bytes: null, total_bytes: null, expire_at: null })).toBe("");
    expect(quotaFraction(SUBS[1]!)).toBeCloseTo(0.86);
    expect(quotaFraction({ up_bytes: 9e9, down_bytes: 9e9, total_bytes: 10e9 })).toBe(1);
    expect(quotaFraction(SUBS[2]!)).toBeNull();
  });

  it("auto-update every N min or off", () => {
    expect(intervalText(3_600)).toBe("every 60 min");
    expect(intervalText(0)).toBe("off");
    expect(intervalText(-60)).toBe("off");
  });

  it("refresh one: the gateway's status, in the error tone when it failed", () => {
    expect(refreshOneMessage("work", { ok: true, status: "ok: +2 ~10 -0", error: null })).toEqual({ text: "work: ok: +2 ~10 -0", error: false });
    expect(refreshOneMessage("home", { ok: false, status: "error: fetch failed: timeout", error: "fetch failed: timeout" })).toEqual({ text: "home: error: fetch failed: timeout", error: true });
    expect(refreshOneMessage("home", { ok: false, error: "fetch failed: timeout" })).toEqual({ text: "home: fetch failed: timeout", error: true });
    expect(refreshOneMessage("work", undefined)).toEqual({ text: "work: refreshed", error: false });
  });

  it("refresh all: counts, and at most three failures by name", () => {
    expect(refreshAllMessage(REFRESH_ALL)).toEqual({ text: "1/2 refreshed · 1 failed — home: fetch failed: timeout", error: true });
    expect(refreshAllMessage({ attempted: 2, succeeded: 2, failed: 0, results: [] })).toEqual({ text: "2/2 refreshed", error: false });
    const many = { attempted: 5, succeeded: 0, failed: 5, results: Object.fromEntries([1, 2, 3, 4, 5].map((id) => [String(id), { id, ok: false, status: "error" }])) };
    expect(refreshAllMessage(many).text).toBe("0/5 refreshed · 5 failed — #1: error · #2: error · #3: error");
    expect(refreshAllMessage({ attempted: 1, succeeded: 0, failed: 1, results: [] }).text).toBe("0/1 refreshed · 1 failed");
  });

  it("delete confirmation word for word", () => {
    expect(deleteSubMessage(SUBS[0]!)).toBe('Delete subscription "work"? Its 6 node(s) are detached to Servers (an active connection is kept).');
  });
});

describe("A6 · unsupported entries", () => {
  it("counts only positive entries and tolerates nothing at all", () => {
    expect(skippedTotal(undefined)).toBe(0);
    expect(skippedTotal({})).toBe(0);
    expect(skippedTotal({ trojan: 3, hysteria2: 2 })).toBe(5);
    expect(skippedTotal({ trojan: -1, ss: 2 })).toBe(2);
  });

  it("names the labels biggest-first, and shows an unknown label as it arrived", () => {
    expect(skippedText({ hysteria2: 1, trojan: 3, invalid: 2 })).toBe("Trojan ×3 · unusable ×2 · Hysteria2 ×1");
    expect(skippedText({ ss: 1, vmess: 1 })).toBe("Shadowsocks ×1 · VMess ×1");   // tie → alphabetical
    expect(skippedText({ "brand-new": 2 })).toBe("brand-new ×2");
    expect(skippedText({})).toBe("");
  });

  it("the import toast says what was left out, and stays quiet when nothing was", () => {
    expect(importedMessage({ added: 2, total: 3, format: "clash", skipped: { trojan: 4 } }))
      .toBe("imported 2/3 node(s) (clash) · 4 unsupported (Trojan ×4)");
    expect(importedMessage({ added: 3, total: 3, format: "clash", skipped: {} }))
      .toBe("imported 3/3 node(s) (clash)");
  });
});
