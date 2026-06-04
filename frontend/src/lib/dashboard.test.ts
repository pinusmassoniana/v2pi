import { describe, it, expect } from "vitest";
import { subWarnings, sparkPath, agoLabel } from "./dashboard";
import type { Subscription } from "./api";

function sub(p: Partial<Subscription>): Subscription {
  return {
    id: 1, name: "S", url: "u", injection: {}, interval_sec: 0, enabled: true,
    default_profile_id: null, last_fetched: null, last_status: null, last_path: null,
    last_error: null, up_bytes: null, down_bytes: null, total_bytes: null, expire_at: null,
    node_count: 0, ...p,
  };
}

const NOW = 1_700_000_000;   // fixed epoch seconds

describe("subWarnings (E)", () => {
  it("flags imminent expiry (warn) and past expiry (bad)", () => {
    const soon = subWarnings([sub({ name: "A", expire_at: NOW + 2 * 86400 })], NOW);
    expect(soon).toEqual([{ name: "A", text: "expires in 2d", level: "warn" }]);
    const gone = subWarnings([sub({ name: "B", expire_at: NOW - 10 })], NOW);
    expect(gone).toEqual([{ name: "B", text: "expired", level: "bad" }]);
  });

  it("flags data-cap usage at 80% (warn) and 100% (bad)", () => {
    const hot = subWarnings([sub({ name: "C", up_bytes: 80, down_bytes: 5, total_bytes: 100 })], NOW);
    expect(hot).toEqual([{ name: "C", text: "85% of data cap", level: "warn" }]);
    const full = subWarnings([sub({ name: "D", up_bytes: 60, down_bytes: 60, total_bytes: 100 })], NOW);
    expect(full).toEqual([{ name: "D", text: "data cap reached", level: "bad" }]);
  });

  it("ignores disabled subs and far-off / capless ones", () => {
    expect(subWarnings([sub({ enabled: false, expire_at: NOW - 1 })], NOW)).toEqual([]);
    expect(subWarnings([sub({ expire_at: NOW + 30 * 86400, total_bytes: 0 })], NOW)).toEqual([]);
  });
});

describe("sparkPath (B)", () => {
  it("returns empty for <2 points", () => {
    expect(sparkPath([], 56, 16)).toBe("");
    expect(sparkPath([5], 56, 16)).toBe("");
  });
  it("maps min→bottom and max→top across the width", () => {
    const p = sparkPath([10, 20, 30], 40, 10);
    expect(p.startsWith("M0.0,10.0")).toBe(true);   // first = min → y=h (bottom)
    expect(p.endsWith("40.0,0.0")).toBe(true);       // last = max → y=0 (top), x=w
  });
  it("handles a flat series without dividing by zero", () => {
    expect(sparkPath([7, 7], 40, 10)).toBe("M0.0,10.0 L40.0,10.0");
  });
});

describe("agoLabel (A)", () => {
  it("formats seconds / minutes / hours", () => {
    expect(agoLabel(NOW - 5, NOW)).toBe("5s ago");
    expect(agoLabel(NOW - 120, NOW)).toBe("2m ago");
    expect(agoLabel(NOW - 7200, NOW)).toBe("2h ago");
    expect(agoLabel(NOW + 100, NOW)).toBe("0s ago");   // clamps future to now
  });
});
