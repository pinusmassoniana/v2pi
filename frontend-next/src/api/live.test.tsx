import { QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../features/home/screens";   // loaded up front: the route's lazy import must not wait on fake timers
import "../features/nodes/screens";
import "../features/tunnel/screens";
import "../features/gateway/screens";
import { STATUS_POLL_MS } from "../app/shell/Shell";
import { NOW_SEC, STATUS, holdWrite, mockApi, mockGateway, mockTunnel } from "../test/fixtures";
import { renderApp } from "../test/renderApp";
import { setViewportWidth } from "../test/viewport";
import { GATEWAY_NETWORK_POLL_MS, NETWORK_POLL_MS, RW_POLL_MS, SLOW_POLL_MS } from "./cadence";
import { ApiError, api } from "./client";
import { NETWORK_WRITE } from "./invalidation";
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
  it("Servers: status 3 s (shell); nodes, node health and subscriptions 30 s; settings and profiles never", async () => {
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

/** Mount a Tunnel `path` on fake timers, wait for `loaded`, then count each read over one minute. */
async function countTunnelReads(path: string, loaded: () => HTMLElement | null) {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
  const api$ = mockTunnel(mockApi());
  renderApp(path);
  for (let i = 0; i < 80 && !loaded(); i++) await act(() => vi.advanceTimersByTimeAsync(25));
  expect(loaded()).not.toBeNull();
  const reads = {
    status: api$.getStatus, routing: api$.getRouting, profiles: api$.listProfiles, settings: api$.getSettings, nodes: api$.listNodes,
    network: api$.getNetwork, routingPresets: api$.listRoutingPresets, profilePresets: api$.listProfilePresets,
  };
  const before = Object.fromEntries(Object.entries(reads).map(([key, spy]) => [key, spy.mock.calls.length]));
  await act(() => vi.advanceTimersByTimeAsync(60_000));
  return Object.fromEntries(Object.entries(reads).map(([key, spy]) => [key, spy.mock.calls.length - before[key]!]));
}

describe("Tunnel polls each key at its owner's cadence and no faster", () => {
  it("Routing: status 3 s (shell), routing 30 s; presets, profiles, settings, nodes and network not at all", async () => {
    const during = await countTunnelReads("/tunnel/routing", () => screen.queryByRole("table", { name: "Routing rules" }));
    expect(during).toEqual({
      status: 60_000 / STATUS_POLL_MS, routing: 60_000 / SLOW_POLL_MS, profiles: 0, settings: 0, nodes: 0, network: 0, routingPresets: 0, profilePresets: 0,
    });
  });

  it("Anti-DPI: status 3 s (shell), profiles 30 s; presets, routing, settings, nodes and network not at all", async () => {
    const during = await countTunnelReads("/tunnel/anti-dpi", () => screen.queryByRole("table", { name: "Profiles" }));
    expect(during).toEqual({
      status: 60_000 / STATUS_POLL_MS, routing: 0, profiles: 60_000 / SLOW_POLL_MS, settings: 0, nodes: 0, network: 0, routingPresets: 0, profilePresets: 0,
    });
  });

  it("Health & failover: status 3 s (shell); settings read once, not polled; nothing else", async () => {
    const during = await countTunnelReads("/tunnel/health", () => screen.queryByRole("region", { name: "Health monitoring" }));
    expect(during).toEqual({
      status: 60_000 / STATUS_POLL_MS, routing: 0, profiles: 0, settings: 0, nodes: 0, network: 0, routingPresets: 0, profilePresets: 0,
    });
  });
});

/** Mount a Gateway `path` on fake timers, wait for `loaded`, then count each read over one minute. */
async function countGatewayReads(path: string, loaded: () => HTMLElement | null, during?: (api$: ReturnType<typeof mockApi>) => void) {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
  const api$ = mockGateway(mockApi());
  const view = renderApp(path);
  for (let i = 0; i < 80 && !loaded(); i++) await act(() => vi.advanceTimersByTimeAsync(25));
  expect(loaded()).not.toBeNull();
  const reads = {
    status: api$.getStatus, network: api$.getNetwork, rw: api$.getRw, settings: api$.getSettings, routing: api$.getRouting,
    nodes: api$.listNodes, profiles: api$.listProfiles,
  };
  const total = (key: keyof typeof reads) => reads[key].mock.calls.length;
  during?.(api$);
  const before = Object.fromEntries(Object.entries(reads).map(([key, spy]) => [key, spy.mock.calls.length]));
  return {
    api$, view, total,
    async count(ms = 60_000) {
      await act(() => vi.advanceTimersByTimeAsync(ms));
      return Object.fromEntries(Object.entries(reads).map(([key, spy]) => [key, spy.mock.calls.length - before[key]!]));
    },
  };
}

describe("Gateway polls each key at its owner's cadence and no faster", () => {
  it("Network: status 3 s (shell), network 5 s, settings read once; remote access, routing, nodes and profiles not at all", async () => {
    const { count, total } = await countGatewayReads("/gateway/network", () => screen.queryByRole("region", { name: "DHCP leases" }));
    expect(await count()).toEqual({
      status: 60_000 / STATUS_POLL_MS, network: 60_000 / GATEWAY_NETWORK_POLL_MS, rw: 0, settings: 0, routing: 0, nodes: 0, profiles: 0,
    });
    expect(total("settings")).toBe(1);
  });

  it("Remote access: status 3 s (shell), remote access 15 s; network and settings not at all", async () => {
    const { count } = await countGatewayReads("/gateway/remote-access", () => screen.queryByRole("table", { name: "Clients" }));
    expect(await count()).toEqual({
      status: 60_000 / STATUS_POLL_MS, network: 0, rw: 60_000 / RW_POLL_MS, settings: 0, routing: 0, nodes: 0, profiles: 0,
    });
  });

  it("Remote access re-reads as soon as the status poll shows the tunnel stopped or started, not at its next poll", async () => {
    const { api$, total } = await countGatewayReads("/gateway/remote-access", () => screen.queryByRole("table", { name: "Clients" }));
    const reads = total("rw");
    api$.getStatus.mockResolvedValue({ ...STATUS, running: false, xray_state: "stopped" });
    await act(() => vi.advanceTimersByTimeAsync(STATUS_POLL_MS));
    await act(() => vi.advanceTimersByTimeAsync(10));
    expect(total("rw")).toBe(reads + 1);
    await act(() => vi.advanceTimersByTimeAsync(STATUS_POLL_MS));
    expect(total("rw")).toBe(reads + 1);   // the same state again is no change
  });

  it("the Network poll pauses while an Apply holds the gateway, and resumes after it", async () => {
    const { view, count } = await countGatewayReads("/gateway/network", () => screen.queryByRole("region", { name: "DHCP leases" }));
    let release: () => Promise<void> = async () => {};
    act(() => { release = holdWrite(view.client, NETWORK_WRITE); });
    await act(() => vi.advanceTimersByTimeAsync(0));
    const held = await count();
    expect(held.network).toBe(0);
    expect(held.status).toBe(60_000 / STATUS_POLL_MS);
    await release();
    const after = await count(60_000);
    expect(after.network).toBe(60_000 / GATEWAY_NETWORK_POLL_MS);
  });
});
