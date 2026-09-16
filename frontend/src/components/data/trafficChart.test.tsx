import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { TrafficSample } from "../../api/traffic";
import { TrafficChart } from "./TrafficChart";
import { MAX_POINTS, axisRate, downsample, niceMax, relativeLabel, trafficGeometry, windowSlice } from "./trafficGeometry";

const T0 = 1_700_000_000_000;
const series = (count: number, stepMs = 1000): TrafficSample[] =>
  Array.from({ length: count }, (_, i) => ({ ts: T0 + i * stepMs, up: 1_000 + i, down: 2_000_000 + i * 10 }));
const pad = (n: number) => String(n).padStart(2, "0");

describe("traffic geometry", () => {
  it("window slice ends at the newest sample and keeps the boundary sample", () => {
    const samples = series(601);
    expect(windowSlice(samples, 60).map((s) => s.ts - T0)).toEqual(Array.from({ length: 61 }, (_, i) => (540 + i) * 1000));
    expect(windowSlice(samples, 3_600)).toHaveLength(601);
    expect(windowSlice([], 600)).toEqual([]);
  });

  it("downsampling never exceeds the point budget and always keeps the newest sample", () => {
    const samples = series(2_000);
    const drawn = downsample(samples, MAX_POINTS);
    expect(drawn).toHaveLength(700);
    expect(drawn.at(-1)).toBe(samples.at(-1));
    expect(downsample(samples.slice(0, 5), MAX_POINTS)).toEqual(samples.slice(0, 5));
  });

  it("round axis maximum and compact labels", () => {
    expect(niceMax(0)).toBe(1);
    expect(niceMax(7)).toBe(10);
    expect(niceMax(18_200_000)).toBe(20_000_000);
    expect(niceMax(1_500)).toBe(2_000);
    expect(niceMax(400)).toBe(500);
    expect([axisRate(20_000_000), axisRate(2_500_000), axisRate(500_000), axisRate(0)]).toEqual(["20M", "2.5M", "500k", "0"]);
    expect([relativeLabel(0, 600), relativeLabel(30, 60), relativeLabel(300, 600), relativeLabel(21_600, 86_400), relativeLabel(259_200, 604_800)])
      .toEqual(["now", "-30s", "-5m", "-6h", "-3d"]);
  });

  it("draws down, its area and up across the window, with five gridlines and a peak position", () => {
    const samples = series(601);
    const g = trafficGeometry(samples, 600, { bps: 2_006_000, ts: T0 + 600_000 }, 184);
    expect(g.points).toBe(601);
    expect(g.lineDown.startsWith("M0.0,")).toBe(true);
    expect(g.lineDown.endsWith(`700.0,${(184 - (2_006_000 / 5_000_000) * 184).toFixed(1)}`)).toBe(true);
    expect(g.areaDown.endsWith("L700.0,184 L0.0,184 Z")).toBe(true);
    expect(g.lineUp.split(" ")).toHaveLength(601);
    expect(g.yTicks).toEqual(["5M", "3.8M", "2.5M", "1.3M", "0"]);
    expect(g.gridY).toEqual([0, 46, 92, 138, 184]);
    expect(g.xTicks).toEqual(["-10m", "-8m", "-5m", "-3m", "now"]);
    expect(g.peak).toEqual({ left: 100, top: expect.closeTo(59.88, 2) });
  });

  it("an idle or one-sample window has no lines and no peak", () => {
    expect(trafficGeometry(series(1), 600, { bps: 1, ts: T0 }, 184)).toMatchObject({ lineDown: "", areaDown: "", lineUp: "", points: 1, peak: null });
  });
});

describe("TrafficChart", () => {
  it("offers the five windows, marks the selected one and reports a switch", async () => {
    const onWindowChange = vi.fn();
    render(<TrafficChart samples={series(10)} windowSec={600} onWindowChange={onWindowChange} peak={null} stale={false} />);
    const group = screen.getByRole("group", { name: "Chart window" });
    expect(within(group).getAllByRole("button").map((b) => [b.textContent, b.getAttribute("aria-pressed")])).toEqual([
      ["1m", "false"], ["10m", "true"], ["1h", "false"], ["24h", "false"], ["7d", "false"],
    ]);
    await userEvent.click(within(group).getByRole("button", { name: "24h" }));
    expect(onWindowChange).toHaveBeenCalledWith(86_400);
  });

  it("plots at most 700 points of each series", () => {
    const { container } = render(<TrafficChart samples={series(3_600)} windowSec={3_600} onWindowChange={() => {}} peak={null} stale={false} />);
    expect(screen.getByRole("img", { name: "Throughput over the last 1h" })).toBeInTheDocument();
    for (const name of ["down", "up"]) {
      const d = container.querySelector(`path[data-series="${name}"]`)!.getAttribute("d")!;
      expect(d.split(" ")).toHaveLength(700);
    }
  });

  it("marks the peak with its rate and local time", () => {
    const peak = { bps: 12_400_000, ts: T0 + 5_000 };
    const at = new Date(peak.ts);
    render(<TrafficChart samples={series(10)} windowSec={60} onWindowChange={() => {}} peak={peak} stale={false} />);
    expect(document.querySelector("[data-peak]")).toHaveTextContent(`peak 12.4 Mbit/s · ${pad(at.getHours())}:${pad(at.getMinutes())}:${pad(at.getSeconds())}`);
  });

  it("dims a stale plot and says why", () => {
    const { container } = render(<TrafficChart samples={series(10)} windowSec={60} onWindowChange={() => {}} peak={null} stale />);
    expect(container.querySelector("[data-stale]")).not.toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("Tunnel health is stale — rates are not proof of a healthy tunnel");
    expect(screen.getByRole("img")).toHaveAccessibleName("Throughput over the last 1m; tunnel health is stale");
  });

  it("waits for traffic below two samples, and a body replaces the plot (and the stale caption)", () => {
    const { rerender } = render(<TrafficChart samples={series(1)} windowSec={600} onWindowChange={() => {}} peak={null} stale={false} />);
    expect(screen.getByText("Waiting for traffic…")).toBeInTheDocument();
    rerender(<TrafficChart samples={series(10)} windowSec={600} onWindowChange={() => {}} peak={null} stale body={<p>Traffic stats are off</p>} />);
    expect(screen.getByText("Traffic stats are off")).toBeInTheDocument();
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByRole("group", { name: "Chart window" })).toBeInTheDocument();
  });
});
