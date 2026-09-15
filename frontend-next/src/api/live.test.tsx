import { QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../features/home/screens";   // loaded up front: the route's lazy import must not wait on fake timers
import "../features/nodes/screens";
import { STATUS_POLL_MS } from "../app/shell/Shell";
import { NOW_SEC, STATUS, mockApi } from "../test/fixtures";
import { renderApp } from "../test/renderApp";
import { setViewportWidth } from "../test/viewport";
import { NETWORK_POLL_MS, SLOW_POLL_MS } from "./cadence";
import { ApiError, api } from "./client";
import { keys, queries } from "./keys";
import { usePolledQuery } from "./live";
import { createQueryClient } from "./queryClient";

afterEach(() => vi.useRealTimers());

function Owner({ intervalMs = 3000 }: { intervalMs?: number }) { usePolledQuery(queries.status(), intervalMs); return null; }
function Reader() { useQuery(queries.status()); return null; }

describe("one polling owner per key", () => {
  it("fetches once per interval however many components show the resource", async () => {
    vi.useFakeTimers();
    const get = vi.spyOn(api, "getStatus").mockResolvedValue(STATUS);
    render(
      <QueryClientProvider client={createQueryClient()}>
        <Owner /><Reader /><Reader />
      </QueryClientProvider>,
    );
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(get).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(9_000));
    expect(get).toHaveBeenCalledTimes(4);
  });

  it("a reader mounted between two polls shows the cache instead of fetching", async () => {
    vi.useFakeTimers();
    const get = vi.spyOn(api, "getStatus").mockResolvedValue(STATUS);
    const client = createQueryClient();
    const view = render(<QueryClientProvider client={client}><Owner /></QueryClientProvider>);
    await act(() => vi.advanceTimersByTimeAsync(2_000));
    view.rerender(<QueryClientProvider client={client}><Owner /><Reader /></QueryClientProvider>);
    await act(() => vi.advanceTimersByTimeAsync(500));
    expect(get).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(1_000));
    expect(get).toHaveBeenCalledTimes(2);
  });

  it("a key nobody polls is refetched by the next component that mounts it", async () => {
    const get = vi.spyOn(api, "getStatus").mockResolvedValue(STATUS);
    const client = createQueryClient();
    const first = render(<QueryClientProvider client={client}><Reader /></QueryClientProvider>);
    await act(() => client.getQueryCache().find({ queryKey: keys.status })!.promise);
    first.unmount();
    render(<QueryClientProvider client={client}><Reader /></QueryClientProvider>);
    await vi.waitFor(() => expect(get).toHaveBeenCalledTimes(2));
  });

  it("a second polling owner overrides the first one's cadence — why each key has one owner", async () => {
    vi.useFakeTimers();
    const get = vi.spyOn(api, "getStatus").mockResolvedValue(STATUS);
    const client = createQueryClient();
    const view = render(<QueryClientProvider client={client}><Owner intervalMs={10_000} /></QueryClientProvider>);
    await act(() => vi.advanceTimersByTimeAsync(1_000));
    // Every query update re-arms both timers, so the key polls every 3 s, not every 10 s.
    view.rerender(<QueryClientProvider client={client}><Owner intervalMs={10_000} /><Owner intervalMs={3_000} /></QueryClientProvider>);
    await act(() => vi.advanceTimersByTimeAsync(20_000));
    expect(get).toHaveBeenCalledTimes(7);
  });

  it("does not retry a 401 — the auth gate owns a lost session", async () => {
    const client = createQueryClient();
    const get = vi.spyOn(api, "listNodes").mockRejectedValue(new ApiError(401, "unauthorized"));
    await expect(client.fetchQuery(queries.nodes())).rejects.toThrow("unauthorized");
    expect(get).toHaveBeenCalledTimes(1);
    expect(client.getQueryState(keys.nodes)?.status).toBe("error");
  });
});

/** Mount `path` on fake timers and let its node health land. */
async function mountHome(path: "/" | "/traffic") {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
  const api$ = mockApi();
  renderApp(path);
  const loaded = () => screen.queryByRole("list", { name: path === "/" ? "Standby nodes" : "Nodes by latency" });
  for (let i = 0; i < 40 && !loaded(); i++) await act(() => vi.advanceTimersByTimeAsync(25));
  expect(loaded()).toBeInTheDocument();
  return api$;
}

describe.each(["/", "/traffic"] as const)("Home %s after the gateway switches node on its own", (path) => {
  it("refetches node health and the network read as soon as the status poll shows it", async () => {
    const api$ = await mountHome(path);
    const health = api$.listNodeHealth.mock.calls.length;
    const network = api$.getNetwork.mock.calls.length;
    api$.getStatus.mockResolvedValue({ ...STATUS, active_node_id: 2, prev_active_node_id: 1, last_failover_at: NOW_SEC + 3 });
    // The next status poll lands well before network's 4 s poll and node health's 30 s one.
    await act(() => vi.advanceTimersByTimeAsync(STATUS_POLL_MS));
    await act(() => vi.advanceTimersByTimeAsync(10));
    expect(api$.listNodeHealth.mock.calls.length).toBe(health + 1);
    expect(api$.getNetwork.mock.calls.length).toBe(network + 1);
  });
});

describe("Home › Traffic polls each key at its owner's cadence and no faster", () => {
  it("status 3 s (shell), network 4 s, node health 30 s; node names are read once, not polled", async () => {
    const api$ = await mountHome("/traffic");
    const reads = { status: api$.getStatus, network: api$.getNetwork, nodes: api$.listNodes, nodeHealth: api$.listNodeHealth };
    const before = Object.fromEntries(Object.entries(reads).map(([key, spy]) => [key, spy.mock.calls.length]));
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    const during = Object.fromEntries(Object.entries(reads).map(([key, spy]) => [key, spy.mock.calls.length - before[key]!]));
    expect(during).toEqual({ status: 60_000 / STATUS_POLL_MS, network: 60_000 / NETWORK_POLL_MS, nodes: 0, nodeHealth: 60_000 / SLOW_POLL_MS });
    expect(api$.listSubs).not.toHaveBeenCalled();
    expect(api$.getRouting).not.toHaveBeenCalled();
    expect(api$.getTrafficHistory.mock.calls.filter(([sec]) => sec === 86_400 || sec === 604_800)).toHaveLength(0);
  });
});

describe("Home › Overview polls each key at its owner's cadence and no faster", () => {
  it("status 3 s (shell), network 4 s, nodes / node health / subscriptions / routing 30 s, long history never", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
    const api$ = mockApi();
    renderApp("/");
    for (let i = 0; i < 40 && !screen.queryByRole("list", { name: "Standby nodes" }); i++) {
      await act(() => vi.advanceTimersByTimeAsync(25));
    }
    expect(screen.getByRole("list", { name: "Standby nodes" })).toBeInTheDocument();

    const reads = {
      status: api$.getStatus, network: api$.getNetwork, nodes: api$.listNodes,
      nodeHealth: api$.listNodeHealth, subs: api$.listSubs, routing: api$.getRouting,
    };
    const before = Object.fromEntries(Object.entries(reads).map(([key, spy]) => [key, spy.mock.calls.length]));
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    const during = Object.fromEntries(Object.entries(reads).map(([key, spy]) => [key, spy.mock.calls.length - before[key]!]));

    expect(during).toEqual({
      status: 60_000 / STATUS_POLL_MS,
      network: 60_000 / NETWORK_POLL_MS,
      nodes: 60_000 / SLOW_POLL_MS,
      nodeHealth: 60_000 / SLOW_POLL_MS,
      subs: 60_000 / SLOW_POLL_MS,
      routing: 60_000 / SLOW_POLL_MS,
    });
    expect(api$.getTrafficHistory.mock.calls.filter(([sec]) => sec === 86_400 || sec === 604_800)).toHaveLength(0);
  });
});

/** Mount `path` on fake timers, wait for `loaded`, then count each read over one minute. */
async function countNodesReads(path: string, loaded: () => HTMLElement | null) {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
  const api$ = mockApi();
  renderApp(path);
  for (let i = 0; i < 80 && !loaded(); i++) await act(() => vi.advanceTimersByTimeAsync(25));
  expect(loaded()).not.toBeNull();
  const reads = {
    status: api$.getStatus, nodes: api$.listNodes, nodeHealth: api$.listNodeHealth, subs: api$.listSubs,
    profiles: api$.listProfiles, settings: api$.getSettings, network: api$.getNetwork,
  };
  const before = Object.fromEntries(Object.entries(reads).map(([key, spy]) => [key, spy.mock.calls.length]));
  await act(() => vi.advanceTimersByTimeAsync(60_000));
  return Object.fromEntries(Object.entries(reads).map(([key, spy]) => [key, spy.mock.calls.length - before[key]!]));
}

describe("Nodes polls each key at its owner's cadence and no faster", () => {
  it("Servers: status 3 s (shell); nodes, node health and subscriptions 30 s; settings read once, profiles never", async () => {
    const during = await countNodesReads("/nodes", () => document.querySelector("tr[data-node-id]"));
    expect(during).toEqual({
      status: 60_000 / STATUS_POLL_MS, nodes: 60_000 / SLOW_POLL_MS, nodeHealth: 60_000 / SLOW_POLL_MS, subs: 60_000 / SLOW_POLL_MS,
      profiles: 0, settings: 0, network: 0,
    });
  });

  it("Subscriptions: status 3 s (shell), subscriptions 30 s; nodes and node health not at all", async () => {
    const during = await countNodesReads("/nodes/subscriptions", () => screen.queryByRole("region", { name: "work" }));
    expect(during).toEqual({ status: 60_000 / STATUS_POLL_MS, nodes: 0, nodeHealth: 0, subs: 60_000 / SLOW_POLL_MS, profiles: 0, settings: 0, network: 0 });
  });

  it("Node detail on a phone: the layout's list polls nodes, node health and subscriptions; the page itself polls nothing", async () => {
    setViewportWidth(390);
    const during = await countNodesReads("/nodes/2", () => screen.queryByRole("region", { name: "Config" }));
    expect(during).toEqual({
      status: 60_000 / STATUS_POLL_MS, nodes: 60_000 / SLOW_POLL_MS, nodeHealth: 60_000 / SLOW_POLL_MS, subs: 60_000 / SLOW_POLL_MS,
      profiles: 0, settings: 0, network: 0,
    });
  });
});
