import { afterEach, describe, expect, it, vi } from "vitest";
import { DENSITY_KEY, listState, nodesSearchSchema, readDense, toSearch, writeDense } from "./search";

afterEach(() => localStorage.clear());

describe("Servers URL state", () => {
  it("keeps valid params, drops anything else, and never fails", () => {
    expect(nodesSearchSchema.parse({ group: "servers", q: "de-fra", sort: "tcp", dir: "desc" })).toEqual({ group: "servers", q: "de-fra", sort: "tcp", dir: "desc" });
    expect(nodesSearchSchema.parse({ group: 2, q: 123 })).toEqual({ group: 2, q: "123" });
    expect(nodesSearchSchema.parse({ group: "2" })).toEqual({ group: 2 });
    expect(nodesSearchSchema.parse({ group: "work", sort: "speed", dir: "up", q: { a: 1 } })).toEqual({});
    expect(nodesSearchSchema.parse({ group: 0 })).toEqual({});
    expect(nodesSearchSchema.parse({})).toEqual({});
  });

  it("fills defaults in and leaves them out again", () => {
    expect(listState({})).toEqual({ group: undefined, q: "", sort: "pos", dir: "asc" });
    expect(toSearch({ group: undefined, q: "", sort: "pos", dir: "asc" })).toEqual({ group: undefined, q: undefined, sort: undefined, dir: undefined });
    expect(toSearch({ group: 2, q: "hel", sort: "name", dir: "desc" })).toEqual({ group: 2, q: "hel", sort: "name", dir: "desc" });
    expect(toSearch(listState({ group: "servers", sort: "http" }))).toEqual({ group: "servers", q: undefined, sort: "http", dir: undefined });
  });
});

describe("density", () => {
  it("is remembered as 1 / 0 under the Svelte panel's key", () => {
    expect(readDense()).toBe(false);
    writeDense(true);
    expect(localStorage.getItem(DENSITY_KEY)).toBe("1");
    expect(readDense()).toBe(true);
    writeDense(false);
    expect(localStorage.getItem("nodes-density")).toBe("0");
    expect(readDense()).toBe(false);
  });

  it("blocked site data reads as comfortable and never throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new DOMException("blocked", "SecurityError"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("blocked", "SecurityError"); });
    expect(readDense()).toBe(false);
    expect(() => writeDense(true)).not.toThrow();
  });
});
