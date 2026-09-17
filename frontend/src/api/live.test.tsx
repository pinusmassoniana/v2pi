import { QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../features/home/screens";   // loaded up front: the route's lazy import must not wait on fake timers
import "../features/nodes/screens";
import "../features/tunnel/screens";
import "../features/gateway/screens";
import "../features/system/screens";
import { SECTIONS } from "../app/nav";
import { STATUS_POLL_MS } from "../app/shell/Shell";
import { NOW_SEC, STATUS, holdWrite, mockApi, mockGateway, mockSystem, mockTunnel } from "../test/fixtures";
import { renderApp } from "../test/renderApp";
import { setViewportWidth } from "../test/viewport";
import { GATEWAY_NETWORK_POLL_MS, LOGS_POLL_MS, NETWORK_POLL_MS, RW_POLL_MS, SLOW_POLL_MS } from "./cadence";
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

/** Mount a System `path` on fake timers, wait for `loaded`, then count each read over one minute. */
async function countSystemReads(path: string, loaded: () => HTMLElement | null) {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
  const api$ = mockSystem(mockApi());
  const view = renderApp(path);
  for (let i = 0; i < 80 && !loaded(); i++) await act(() => vi.advanceTimersByTimeAsync(25));
  expect(loaded()).not.toBeNull();
  const reads = {
    status: api$.getStatus, settings: api$.getSettings, tokens: api$.listTokens, audit: api$.listAudit,
    diagnostics: api$.getDiagnostics, logs: api$.getLogs, network: api$.getNetwork, nodes: api$.listNodes,
    subs: api$.listSubs, profiles: api$.listProfiles, routing: api$.getRouting,
  };
  const total = (key: keyof typeof reads) => reads[key].mock.calls.length;
  const before = Object.fromEntries(Object.entries(reads).map(([key, spy]) => [key, spy.mock.calls.length]));
  return {
    api$, view, total,
    async count(ms = 60_000) {
      await act(() => vi.advanceTimersByTimeAsync(ms));
      return Object.fromEntries(Object.entries(reads).map(([key, spy]) => [key, spy.mock.calls.length - before[key]!]));
    },
  };
}

describe("System polls each key at its owner's cadence and no faster", () => {
  it("Backups: status 3 s (shell), settings 30 s, and the four lists the file-holds card counts exactly once each", async () => {
    const { count, total } = await countSystemReads("/system/backups", () => screen.queryByRole("region", { name: "What the file holds" }));
    expect(await count()).toEqual({
      status: 60_000 / STATUS_POLL_MS, settings: 60_000 / SLOW_POLL_MS,
      nodes: 0, subs: 0, profiles: 0, routing: 0,
      tokens: 0, audit: 0, diagnostics: 0, logs: 0, network: 0,
    });
    // a key with no interval is read once on mount and never again — the price of the approved card
    for (const key of ["nodes", "subs", "profiles", "routing"] as const) expect(total(key)).toBe(1);
  });

  it("Access: status 3 s, settings and tokens 30 s, and the audit log not at all until Show", async () => {
    const { count } = await countSystemReads("/system/access", () => screen.queryByRole("region", { name: "API tokens" }));
    expect(await count()).toEqual({
      status: 60_000 / STATUS_POLL_MS, settings: 60_000 / SLOW_POLL_MS, tokens: 60_000 / SLOW_POLL_MS,
      audit: 0, diagnostics: 0, logs: 0, network: 0, nodes: 0, subs: 0, profiles: 0, routing: 0,
    });
  });

  it("Logs: status 3 s and nothing else before Load; after it, only while auto-refresh is on", async () => {
    const { count, view } = await countSystemReads("/system/logs", () => screen.queryByRole("region", { name: "Log output" }));
    expect(await count()).toEqual({
      status: 60_000 / STATUS_POLL_MS,
      logs: 0, settings: 0, tokens: 0, audit: 0, diagnostics: 0, network: 0, nodes: 0, subs: 0, profiles: 0, routing: 0,
    });

    const button = (name: RegExp | string) => screen.getByRole("button", { name });
    await act(async () => { button(/^(Load|Loading…)$/).click(); });
    await act(() => vi.advanceTimersByTimeAsync(10));
    const afterLoad = await count();
    expect(afterLoad.logs).toBe(1);        // the read Load asked for, and no interval behind it

    await act(async () => { screen.getByRole("switch", { name: "Auto-refresh" }).click(); });
    await act(() => vi.advanceTimersByTimeAsync(10));
    const polling = await count();
    // one read the moment the interval is armed (the key went stale when its freshness became 5 s), then one per tick
    expect(polling.logs).toBe(1 + 60_000 / LOGS_POLL_MS);
    expect(view.client.getQueryCache().getAll().filter((query) => query.queryKey[0] === "logs" && query.observers.length > 0)).toHaveLength(1);
  });

  it("Panel: status 3 s, settings and diagnostics 30 s; tokens, audit and logs not at all", async () => {
    const { count } = await countSystemReads("/system/panel", () => screen.queryByRole("region", { name: "System" }));
    expect(await count()).toEqual({
      status: 60_000 / STATUS_POLL_MS, settings: 60_000 / SLOW_POLL_MS, diagnostics: 60_000 / SLOW_POLL_MS,
      tokens: 0, audit: 0, logs: 0, network: 0, nodes: 0, subs: 0, profiles: 0, routing: 0,
    });
  });
});

/**
 * The api read behind each key root in keys.ts. `trafficHistory` counts every read of the history endpoint, including
 * the live traffic store's one seed of its last hour when a screen first shows live traffic.
 */
const READ_OF = {
  status: "getStatus", nodes: "listNodes", nodeHealth: "listNodeHealth", subs: "listSubs", profiles: "listProfiles",
  profilePresets: "listProfilePresets", routing: "getRouting", routingPresets: "listRoutingPresets", network: "getNetwork",
  rw: "getRw", settings: "getSettings", tokens: "listTokens", diagnostics: "getDiagnostics", geo: "getGeo", events: "listEvents", trafficUsage: "getTrafficUsage",
  reservations: "listReservations", audit: "listAudit",
  logs: "getLogs", trafficHistory: "getTrafficHistory",
} as const satisfies Record<keyof typeof keys, keyof ReturnType<typeof mockSystem>>;

type KeyRoot = keyof typeof READ_OF;

const MINUTE = 60_000;
const SHELL = MINUTE / STATUS_POLL_MS;
const SLOW = MINUTE / SLOW_POLL_MS;
const HOME_NETWORK = MINUTE / NETWORK_POLL_MS;
const GATEWAY_NETWORK = MINUTE / GATEWAY_NETWORK_POLL_MS;
const REMOTE = MINUTE / RW_POLL_MS;

/**
 * The whole app's polling table: how many times each key is read in the minute after a route has loaded, for all 13
 * tabs and for node detail on a phone. A key's cell is its owner's cadence or 0; no route polls a key faster than its
 * owner, and no route polls a key another section owns.
 */
const POLLING: Record<string, Record<KeyRoot, number>> = {
  "/": { status: SHELL, nodes: SLOW, nodeHealth: SLOW, subs: SLOW, profiles: 0, profilePresets: 0, routing: SLOW, routingPresets: 0, network: HOME_NETWORK, rw: 0, settings: 0, tokens: 0, diagnostics: 0, geo: 0, events: 0, trafficUsage: 0, reservations: 0, audit: 0, logs: 0, trafficHistory: 0 },
  "/traffic": { status: SHELL, nodes: 0, nodeHealth: SLOW, subs: 0, profiles: 0, profilePresets: 0, routing: 0, routingPresets: 0, network: HOME_NETWORK, rw: 0, settings: 0, tokens: 0, diagnostics: 0, geo: 0, events: 0, trafficUsage: 0, reservations: 0, audit: 0, logs: 0, trafficHistory: 0 },
  "/nodes": { status: SHELL, nodes: SLOW, nodeHealth: SLOW, subs: SLOW, profiles: 0, profilePresets: 0, routing: 0, routingPresets: 0, network: 0, rw: 0, settings: 0, tokens: 0, diagnostics: 0, geo: 0, events: 0, trafficUsage: 0, reservations: 0, audit: 0, logs: 0, trafficHistory: 0 },
  "/nodes/subscriptions": { status: SHELL, nodes: 0, nodeHealth: 0, subs: SLOW, profiles: 0, profilePresets: 0, routing: 0, routingPresets: 0, network: 0, rw: 0, settings: 0, tokens: 0, diagnostics: 0, geo: 0, events: 0, trafficUsage: 0, reservations: 0, audit: 0, logs: 0, trafficHistory: 0 },
  "/nodes/1": { status: SHELL, nodes: SLOW, nodeHealth: SLOW, subs: SLOW, profiles: 0, profilePresets: 0, routing: 0, routingPresets: 0, network: 0, rw: 0, settings: 0, tokens: 0, diagnostics: 0, geo: 0, events: 0, trafficUsage: 0, reservations: 0, audit: 0, logs: 0, trafficHistory: 0 },
  "/tunnel/routing": { status: SHELL, nodes: 0, nodeHealth: 0, subs: 0, profiles: 0, profilePresets: 0, routing: SLOW, routingPresets: 0, network: 0, rw: 0, settings: 0, tokens: 0, diagnostics: 0, geo: 0, events: 0, trafficUsage: 0, reservations: 0, audit: 0, logs: 0, trafficHistory: 0 },
  "/tunnel/anti-dpi": { status: SHELL, nodes: 0, nodeHealth: 0, subs: 0, profiles: SLOW, profilePresets: 0, routing: 0, routingPresets: 0, network: 0, rw: 0, settings: 0, tokens: 0, diagnostics: 0, geo: 0, events: 0, trafficUsage: 0, reservations: 0, audit: 0, logs: 0, trafficHistory: 0 },
  "/tunnel/health": { status: SHELL, nodes: 0, nodeHealth: 0, subs: 0, profiles: 0, profilePresets: 0, routing: 0, routingPresets: 0, network: 0, rw: 0, settings: 0, tokens: 0, diagnostics: 0, geo: 0, events: 0, trafficUsage: 0, reservations: 0, audit: 0, logs: 0, trafficHistory: 0 },
  "/gateway/network": { status: SHELL, nodes: 0, nodeHealth: 0, subs: 0, profiles: 0, profilePresets: 0, routing: 0, routingPresets: 0, network: GATEWAY_NETWORK, rw: 0, settings: 0, tokens: 0, diagnostics: 0, geo: 0, events: 0, trafficUsage: 0, reservations: 0, audit: 0, logs: 0, trafficHistory: 0 },
  "/gateway/remote-access": { status: SHELL, nodes: 0, nodeHealth: 0, subs: 0, profiles: 0, profilePresets: 0, routing: 0, routingPresets: 0, network: 0, rw: REMOTE, settings: 0, tokens: 0, diagnostics: 0, geo: 0, events: 0, trafficUsage: 0, reservations: 0, audit: 0, logs: 0, trafficHistory: 0 },
  "/system/backups": { status: SHELL, nodes: 0, nodeHealth: 0, subs: 0, profiles: 0, profilePresets: 0, routing: 0, routingPresets: 0, network: 0, rw: 0, settings: SLOW, tokens: 0, diagnostics: 0, geo: 0, events: 0, trafficUsage: 0, reservations: 0, audit: 0, logs: 0, trafficHistory: 0 },
  "/system/access": { status: SHELL, nodes: 0, nodeHealth: 0, subs: 0, profiles: 0, profilePresets: 0, routing: 0, routingPresets: 0, network: 0, rw: 0, settings: SLOW, tokens: SLOW, diagnostics: 0, geo: 0, events: 0, trafficUsage: 0, reservations: 0, audit: 0, logs: 0, trafficHistory: 0 },
  "/system/logs": { status: SHELL, nodes: 0, nodeHealth: 0, subs: 0, profiles: 0, profilePresets: 0, routing: 0, routingPresets: 0, network: 0, rw: 0, settings: 0, tokens: 0, diagnostics: 0, geo: 0, events: 0, trafficUsage: 0, reservations: 0, audit: 0, logs: 0, trafficHistory: 0 },
  "/system/panel": { status: SHELL, nodes: 0, nodeHealth: 0, subs: 0, profiles: 0, profilePresets: 0, routing: 0, routingPresets: 0, network: 0, rw: 0, settings: SLOW, tokens: 0, diagnostics: SLOW, geo: 0, events: 0, trafficUsage: 0, reservations: 0, audit: 0, logs: 0, trafficHistory: 0 },
};

/** The keys a route reads exactly once, on mount, and never again. Every other 0 in its row is never read at all. */
const READ_ONCE: Record<string, readonly KeyRoot[]> = {
  "/": ["trafficHistory"],
  "/traffic": ["nodes", "trafficHistory", "trafficUsage"],
  "/nodes": ["trafficHistory"],
  "/nodes/subscriptions": ["settings"],
  "/nodes/1": ["profiles", "trafficHistory"],
  "/tunnel/routing": ["routingPresets", "geo", "reservations"],
  "/tunnel/anti-dpi": ["profilePresets"],
  "/tunnel/health": ["events", "settings"],
  "/gateway/network": ["reservations", "settings"],
  "/gateway/remote-access": [],
  "/system/backups": ["nodes", "subs", "profiles", "routing"],
  "/system/access": [],
  "/system/logs": [],
  "/system/panel": ["geo"],
};

/** What shows a route has loaded; node detail is mounted at phone width, where it is a page of its own. */
const LOADED: Record<string, { landmark: () => Element | null; phone?: true }> = {
  "/": { landmark: () => screen.queryByRole("list", { name: "Standby nodes" }) },
  "/traffic": { landmark: () => screen.queryByRole("list", { name: "Nodes by latency" }) },
  "/nodes": { landmark: () => document.querySelector("tr[data-node-id]") },
  "/nodes/subscriptions": { landmark: () => screen.queryByRole("region", { name: "work" }) },
  "/nodes/1": { landmark: () => screen.queryByRole("region", { name: "Config" }), phone: true },
  "/tunnel/routing": { landmark: () => screen.queryByRole("table", { name: "Routing rules" }) },
  "/tunnel/anti-dpi": { landmark: () => screen.queryByRole("table", { name: "Profiles" }) },
  "/tunnel/health": { landmark: () => screen.queryByRole("region", { name: "Health monitoring" }) },
  "/gateway/network": { landmark: () => screen.queryByRole("region", { name: "DHCP leases" }) },
  "/gateway/remote-access": { landmark: () => screen.queryByRole("table", { name: "Clients" }) },
  "/system/backups": { landmark: () => screen.queryByRole("region", { name: "What the file holds" }) },
  "/system/access": { landmark: () => screen.queryByRole("region", { name: "API tokens" }) },
  "/system/logs": { landmark: () => screen.queryByRole("region", { name: "Log output" }) },
  "/system/panel": { landmark: () => screen.queryByRole("region", { name: "System" }) },
};

describe("the whole app polls each key at its one owner's cadence", () => {
  it("has a row for every tab and node detail, and a column for every key root", () => {
    const routes = [...SECTIONS.flatMap((section) => section.tabs.map((tab) => tab.to)), "/nodes/1"].sort();
    expect(Object.keys(POLLING).sort()).toEqual(routes);
    expect(Object.keys(READ_ONCE).sort()).toEqual(routes);
    expect(Object.keys(LOADED).sort()).toEqual(routes);
    expect(Object.keys(READ_OF)).toEqual(Object.keys(keys));
    for (const [path, row] of Object.entries(POLLING)) {
      expect(Object.keys(row), path).toEqual(Object.keys(keys));
      // a key read once is not polled
      for (const key of READ_ONCE[path]!) expect(row[key], `${path} ${key}`).toBe(0);
    }
  });

  it.each(Object.keys(POLLING))("%s", async (path) => {
    const { landmark, phone } = LOADED[path]!;
    if (phone) setViewportWidth(390);
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
    const api$ = mockSystem(mockGateway(mockTunnel(mockApi())));
    renderApp(path);
    for (let i = 0; i < 80 && !landmark(); i++) await act(() => vi.advanceTimersByTimeAsync(25));
    expect(landmark()).not.toBeNull();

    const columns = Object.keys(READ_OF) as KeyRoot[];
    const reads = () => Object.fromEntries(columns.map((key) => [key, api$[READ_OF[key]].mock.calls.length])) as Record<KeyRoot, number>;
    const mounted = reads();
    await act(() => vi.advanceTimersByTimeAsync(MINUTE));
    const total = reads();

    expect(Object.fromEntries(columns.map((key) => [key, total[key] - mounted[key]]))).toEqual(POLLING[path]);
    const unpolled = columns.filter((key) => POLLING[path]![key] === 0);
    expect(Object.fromEntries(unpolled.map((key) => [key, total[key]])))
      .toEqual(Object.fromEntries(unpolled.map((key) => [key, READ_ONCE[path]!.includes(key) ? 1 : 0])));
  });
});
