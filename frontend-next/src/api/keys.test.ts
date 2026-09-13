import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { STATUS } from "../test/fixtures";
import { api } from "./client";
import { resetClock, serverNow } from "./clock";
import { keys, queries } from "./keys";

afterEach(() => { resetClock(); vi.useRealTimers(); });

type ApiMethod = (...args: readonly unknown[]) => Promise<unknown>;
const apiMocks = api as unknown as Record<string, ApiMethod>;

type Case = [name: string, options: () => { queryKey: readonly unknown[] }, method: keyof typeof api, args: unknown[]];
const cases: Case[] = [
  ["nodes", () => queries.nodes(), "listNodes", []],
  ["nodeHealth", () => queries.nodeHealth(), "listNodeHealth", []],
  ["subs", () => queries.subs(), "listSubs", []],
  ["profiles", () => queries.profiles(), "listProfiles", []],
  ["profilePresets", () => queries.profilePresets(), "listProfilePresets", []],
  ["routing", () => queries.routing(), "getRouting", []],
  ["routingPresets", () => queries.routingPresets(), "listRoutingPresets", []],
  ["network", () => queries.network(), "getNetwork", []],
  ["rw", () => queries.rw(), "getRw", []],
  ["settings", () => queries.settings(), "getSettings", []],
  ["tokens", () => queries.tokens(), "listTokens", []],
  ["diagnostics", () => queries.diagnostics(), "getDiagnostics", []],
  ["audit", () => queries.audit(), "listAudit", []],
  ["logs", () => queries.logs("app", 200), "getLogs", ["app", 200]],
];

describe("query options", () => {
  it.each(cases)("%s reads its resource through the API client", async (_name, options, method, args) => {
    const sentinel = { marker: method };
    const spy = vi.spyOn(apiMocks, method).mockResolvedValue(sentinel);
    const client = new QueryClient();
    await expect(client.fetchQuery(options() as never)).resolves.toBe(sentinel);
    expect(spy).toHaveBeenCalledWith(...args);
  });

  it("status records the gateway clock on every successful read", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(1_700_000_200_000));
    vi.spyOn(api, "getStatus").mockResolvedValue(STATUS);
    await new QueryClient().fetchQuery(queries.status());
    expect(serverNow()).toBe(1_700_000_100_000);
  });

  it("trafficHistory asks for the window at 1200 points", async () => {
    const spy = vi.spyOn(api, "getTrafficHistory").mockResolvedValue({ samples: [], interval_ms: 1000 });
    await new QueryClient().fetchQuery(queries.trafficHistory(86400));
    expect(spy.mock.calls[0]!.slice(0, 2)).toEqual([86400, 1200]);
  });

  it("keys are stable, distinct tuples", () => {
    expect(keys.logs("app", 200)).toEqual(["logs", "app", 200]);
    expect(keys.trafficHistory(600)).toEqual(["trafficHistory", 600]);
    const names = [keys.status, keys.nodes, keys.nodeHealth, keys.subs, keys.profiles, keys.profilePresets,
      keys.routing, keys.routingPresets, keys.network, keys.rw, keys.settings, keys.tokens, keys.diagnostics,
      keys.audit].map((k) => k[0]);
    expect(new Set(names).size).toBe(names.length);
  });
});
