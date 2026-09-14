import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, renderHook, screen } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import { createQueryClient } from "../../api/queryClient";
import type { TrafficWindowSec } from "../../components/data/TrafficChart";
import { TRAFFIC_FRAME, mockApi } from "../../test/fixtures";
import { HISTORY_POLL_MS } from "./cadence";
import { useTrafficSeries, type TrafficSeries } from "./useTrafficSeries";

let latest: TrafficSeries | null = null;
const report = (series: TrafficSeries) => { latest = series; };

// Module scope: renders the hook and hands its last result to the test.
function SeriesProbe({ windowSec }: { windowSec: TrafficWindowSec }) {
  const series = useTrafficSeries(windowSec);
  report(series);
  return <p data-testid="series">{series.samples.length}</p>;
}

const frame = (ts: number, down: number) => ({ ...TRAFFIC_FRAME, ts, outbounds: { proxy: { up_bps: 1, down_bps: down } } });
const historyCalls = (spy: ReturnType<typeof mockApi>["getTrafficHistory"], windowSec: number) =>
  spy.mock.calls.filter(([sec, points, signal]) => sec === windowSec && points === 1200 && signal !== undefined).length;

afterEach(() => {
  vi.useRealTimers();
  latest = null;
});

describe("useTrafficSeries", () => {
  it("short windows come from the live store, sliced to the window, and never ask for recorded history", () => {
    const api$ = mockApi();
    render(<QueryClientProvider client={createQueryClient()}><SeriesProbe windowSec={60} /></QueryClientProvider>);
    const t = TRAFFIC_FRAME.ts;
    for (const [offset, down] of [[0, 1], [30_000, 2], [90_000, 3]] as const) api$.emitTraffic(frame(t + offset, down));
    expect(latest!.samples.map((s) => s.down)).toEqual([2, 3]);
    expect(latest!.live?.ts).toBe(t + 90_000);
    expect(latest!.pending).toBe(false);
    expect(historyCalls(api$.getTrafficHistory, 86_400) + historyCalls(api$.getTrafficHistory, 604_800)).toBe(0);
  });

  it("24 h reads recorded history every 60 s only while it is selected", async () => {
    vi.useFakeTimers();
    const api$ = mockApi();
    api$.getTrafficHistory.mockResolvedValue({ samples: [[T(0), 10, 20], [T(60), 11, 21]], interval_ms: 60_000 });
    const client = createQueryClient();
    const view = render(<QueryClientProvider client={client}><SeriesProbe windowSec={86_400} /></QueryClientProvider>);
    expect(latest!.pending).toBe(true);
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(historyCalls(api$.getTrafficHistory, 86_400)).toBe(1);
    expect(latest!.samples).toEqual([{ ts: T(0), up: 10, down: 20 }, { ts: T(60), up: 11, down: 21 }]);
    expect(latest!.pending).toBe(false);

    await act(() => vi.advanceTimersByTimeAsync(2 * HISTORY_POLL_MS));
    expect(historyCalls(api$.getTrafficHistory, 86_400)).toBe(3);

    view.rerender(<QueryClientProvider client={client}><SeriesProbe windowSec={600} /></QueryClientProvider>);
    await act(() => vi.advanceTimersByTimeAsync(5 * HISTORY_POLL_MS));
    expect(historyCalls(api$.getTrafficHistory, 86_400)).toBe(3);
    expect(historyCalls(api$.getTrafficHistory, 600)).toBe(0);
  });

  it("a failed history read is reported with a retry", async () => {
    vi.useFakeTimers();
    const api$ = mockApi();
    api$.getTrafficHistory.mockRejectedValue(new ApiError(500, "history unavailable"));
    render(<QueryClientProvider client={createQueryClient()}><SeriesProbe windowSec={604_800} /></QueryClientProvider>);
    await act(() => vi.advanceTimersByTimeAsync(5_000));   // the client retries once before giving up
    expect(latest!.error?.message).toBe("history unavailable");
    api$.getTrafficHistory.mockResolvedValue({ samples: [], interval_ms: 60_000 });
    await act(async () => { latest!.retry(); await vi.advanceTimersByTimeAsync(0); });
    expect(latest!.error).toBeNull();
    expect(screen.getByTestId("series")).toHaveTextContent("0");
  });

  it("reports stats switched off and drops the last frame", () => {
    const api$ = mockApi();
    render(<QueryClientProvider client={createQueryClient()}><SeriesProbe windowSec={600} /></QueryClientProvider>);
    api$.emitTraffic(TRAFFIC_FRAME);
    api$.emitTraffic({ disabled: true });
    expect(latest!.disabled).toBe(true);
    expect(latest!.live).toBeNull();
  });

  it("keeps the same samples reference across live frames while a long window is selected", async () => {
    vi.useFakeTimers();
    const api$ = mockApi();
    api$.getTrafficHistory.mockResolvedValue({ samples: [[T(0), 10, 20], [T(60), 11, 21]], interval_ms: 60_000 });
    const client = createQueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client }, children);
    const { result } = renderHook(() => useTrafficSeries(86_400), { wrapper });
    await act(() => vi.advanceTimersByTimeAsync(0));
    const samples = result.current.samples;
    expect(samples.length).toBeGreaterThan(0);

    const t = TRAFFIC_FRAME.ts;
    for (const [offset, down] of [[0, 1], [30_000, 2], [90_000, 3]] as const) api$.emitTraffic(frame(t + offset, down));
    expect(result.current.samples).toBe(samples);
  });

  it("gives a new samples reference on each live frame while a short window is selected", () => {
    const api$ = mockApi();
    const wrapper = ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client: createQueryClient() }, children);
    const { result } = renderHook(() => useTrafficSeries(600), { wrapper });
    const before = result.current.samples;
    api$.emitTraffic(TRAFFIC_FRAME);
    expect(result.current.samples).not.toBe(before);
  });
});

function T(minutes: number): number {
  return TRAFFIC_FRAME.ts - 86_400_000 + minutes * 60_000;
}
