import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TRAFFIC_FRAME, mockApi } from "../test/fixtures";
import type { TrafficFrame } from "./client";
import { IDLE_CLOSE_MS, useTrafficSelect, type TrafficSnapshot } from "./traffic";

afterEach(() => vi.useRealTimers());

const probe = (patch: Partial<NonNullable<TrafficFrame["active"]>>, ts = TRAFFIC_FRAME.ts): TrafficFrame => ({
  ...TRAFFIC_FRAME, ts, active: { ...TRAFFIC_FRAME.active!, ...patch },
});

// Module scope: selectors and components are stable across renders, as the screens declare them.
const selectLatency = (snapshot: TrafficSnapshot) => snapshot.live?.active?.latency_ms ?? null;
const selectHistory = (snapshot: TrafficSnapshot) => snapshot.live?.active?.lat_history ?? [];
const sameValues = (a: readonly number[], b: readonly number[]) => a.length === b.length && a.every((value, i) => value === b[i]);

function Latency({ onRender }: { onRender: () => void }) {
  const ms = useTrafficSelect(selectLatency);
  onRender();
  return <span>latency {ms ?? "—"}</span>;
}

function History({ onRender }: { onRender: (values: readonly number[]) => void }) {
  const values = useTrafficSelect(selectHistory, sameValues);
  onRender(values);
  return <span>history {values.join(",")}</span>;
}

describe("useTrafficSelect", () => {
  it("re-renders only when the selected value changes, not on every frame", () => {
    const api$ = mockApi();
    const onRender = vi.fn();
    render(<Latency onRender={onRender} />);
    expect(screen.getByText("latency —")).toBeInTheDocument();
    api$.emitTraffic(TRAFFIC_FRAME);
    expect(screen.getByText("latency 42")).toBeInTheDocument();
    const renders = onRender.mock.calls.length;
    for (let i = 1; i <= 5; i++) api$.emitTraffic({ ...TRAFFIC_FRAME, ts: TRAFFIC_FRAME.ts + i * 1_000 });
    expect(onRender).toHaveBeenCalledTimes(renders);
    api$.emitTraffic(probe({ latency_ms: 51 }, TRAFFIC_FRAME.ts + 6_000));
    expect(screen.getByText("latency 51")).toBeInTheDocument();
    expect(onRender).toHaveBeenCalledTimes(renders + 1);
  });

  it("keeps the previous selection when isEqual says a new one is the same", () => {
    const api$ = mockApi();
    const seen: (readonly number[])[] = [];
    render(<History onRender={(values) => seen.push(values)} />);
    api$.emitTraffic(TRAFFIC_FRAME);
    const first = seen.at(-1)!;
    api$.emitTraffic(probe({ lat_history: [...TRAFFIC_FRAME.active!.lat_history] }, TRAFFIC_FRAME.ts + 1_000));
    expect(seen.at(-1)).toBe(first);
    api$.emitTraffic(probe({ lat_history: [...TRAFFIC_FRAME.active!.lat_history, 60] }, TRAFFIC_FRAME.ts + 2_000));
    expect(screen.getByText(`history ${[...TRAFFIC_FRAME.active!.lat_history, 60].join(",")}`)).toBeInTheDocument();
  });

  it("opens the live stream like useTraffic, and lets it close a while after the last reader leaves", () => {
    vi.useFakeTimers();
    const api$ = mockApi();
    const view = render(<Latency onRender={() => {}} />);
    expect(api$.connectTraffic).toHaveBeenCalledTimes(1);
    const handle = api$.connectTraffic.mock.results[0]!.value as { close: () => void };
    view.unmount();
    act(() => { vi.advanceTimersByTime(IDLE_CLOSE_MS); });
    expect(handle.close).toHaveBeenCalledTimes(1);
  });
});
