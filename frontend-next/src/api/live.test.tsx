import { QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { STATUS } from "../test/fixtures";
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
