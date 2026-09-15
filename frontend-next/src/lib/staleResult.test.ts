import { describe, expect, it } from "vitest";
import { FORM_CHANGED, checkResult, isStaleRun, runKey } from "./staleResult";

describe("stale peek results", () => {
  it("keys an input by its content, so an equal input made again matches", () => {
    expect(runKey({ url: "https://a.example", injection: { headers: {} } })).toBe(runKey({ url: "https://a.example", injection: { headers: {} } }));
    expect(runKey({ rules: [{ value: "ru" }] })).not.toBe(runKey({ rules: [{ value: "cn" }] }));
  });

  it("a result is stale only when there is one and its input is not the form's input now", () => {
    const ran = runKey({ name: "a" });
    expect(isStaleRun(ran, runKey({ name: "a" }))).toBe(false);
    expect(isStaleRun(ran, runKey({ name: "b" }))).toBe(true);
    expect(isStaleRun(null, runKey({ name: "b" }))).toBe(false);
    expect(isStaleRun(undefined, runKey({ name: "b" }))).toBe(false);
  });

  it("says the form changed in the words the subscription dry-run already used", () => {
    expect(FORM_CHANGED).toBe("Form changed since this run — run it again.");
  });

  it("turns a validate reply into its result line", () => {
    expect(checkResult({ ok: true, error: "" }, "ruleset valid", "k")).toEqual({ ok: true, text: "✓ ruleset valid", key: "k" });
    expect(checkResult({ ok: false, error: "rule 2: bad port '0'" }, "ruleset valid", "k")).toEqual({ ok: false, text: "✗ rule 2: bad port '0'", key: "k" });
  });
});
