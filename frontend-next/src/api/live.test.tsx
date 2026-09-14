import { QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NETWORK_POLL_MS, SLOW_POLL_MS } from "../features/home/cadence";
import "../features/home/screens";   // loaded up front: the route's lazy import must not wait on fake timers
import { STATUS_POLL_MS } from "../app/shell/Shell";
import { STATUS, mockApi } from "../test/fixtures";
import { renderApp } from "../test/renderApp";
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
