import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LatencyBars } from "./LatencyBars";
import { LatencyChart } from "./LatencyChart";
import { latencyGeometry } from "./latencyGeometry";
import type { LatencyRowData } from "./types";

const row = (patch: Partial<LatencyRowData> & Pick<LatencyRowData, "id" | "name" | "state">): LatencyRowData => ({
  flag: "", ms: null, age: null, dim: false, active: false, ...patch,
});

const ROWS: LatencyRowData[] = [
  row({ id: 1, name: "nl-ams-03", flag: "🇳🇱", state: "live", ms: 42, active: true }),
  row({ id: 2, name: "de-fra-01", flag: "🇩🇪", state: "measured", ms: 58, age: "4 min" }),
  row({ id: 4, name: "pl-waw-01", flag: "🇵🇱", state: "measured", ms: 164, age: "23 min", dim: true }),
  row({ id: 6, name: "se-sto-01", flag: "🇸🇪", state: "failed", age: "12 min", dim: true }),
  row({ id: 5, name: "ch-zrh-02", state: "not-probed" }),
];

const items = () => within(screen.getByRole("list", { name: "Upstream latency" })).getAllByRole("listitem");

describe("LatencyBars", () => {
  it("one row per node: name with flag, value and the probe's age", () => {
    render(<LatencyBars rows={ROWS} label="Upstream latency" />);
    expect(items().map((li) => li.textContent)).toEqual([
      "🇳🇱 nl-ams-0342 mslive",
      "🇩🇪 de-fra-0158 ms· 4 min",
      "🇵🇱 pl-waw-01164 ms· 23 min",
      "🇸🇪 se-sto-01failed· 12 min",
      "ch-zrh-02not probed",
    ]);
  });

  it("bars on a shared 0–250 ms scale: brand for the active node, amber above 150 ms, none without a number", () => {
    render(<LatencyBars rows={ROWS} label="Upstream latency" />);
    const [live, fast, slow, failed, none] = items().map((li) => li.querySelector("i"));
    expect(live).toHaveClass("bg-brand");
    expect(live).toHaveStyle({ width: "16.8%" });
    expect(fast).toHaveStyle({ width: "23.2%" });
    expect(slow).toHaveClass("bg-warn");
    expect(within(items()[2]!).getByText("164 ms")).toHaveClass("text-warn");
    expect(failed).toBeNull();
    expect(none).toBeNull();
    expect(items()[3]!.querySelector(".track-failed")).not.toBeNull();
    expect(items()[4]!.querySelector(".track-none")).not.toBeNull();
  });

  it("marks state, active and dimmed rows, and caps a bar at full scale", () => {
    render(<LatencyBars rows={[...ROWS, row({ id: 9, name: "far", state: "measured", ms: 900, age: "1 min" })]} label="Upstream latency" />);
    expect(items().map((li) => [li.dataset.state, li.dataset.active ?? "", li.dataset.dim ?? ""])).toEqual([
      ["live", "true", ""], ["measured", "", ""], ["measured", "", "true"], ["failed", "", "true"], ["not-probed", "", ""], ["measured", "", ""],
    ]);
    expect(items()[5]!.querySelector("i")).toHaveStyle({ width: "100%" });
  });

  it("the active node's own failure and staleness read as sentences", () => {
    render(
      <LatencyBars
        rows={[row({ id: 1, name: "a", state: "failed", active: true }), row({ id: 2, name: "b", state: "stale", active: true })]}
        label="Upstream latency"
      />,
    );
    expect(screen.getByText("probe failed")).toHaveClass("text-bad");
    expect(screen.getByText("health stale")).toHaveClass("text-warn");
  });

  it("draws the threshold line and the axis it is given", () => {
    const { container } = render(<LatencyBars rows={ROWS.slice(0, 1)} label="Upstream latency" ticks={[0, 50, 100, 150, 200, 250]} thresholdLine />);
    expect(container.querySelector("li span.bg-warn\\/50")).toHaveStyle({ left: "60%" });
    expect(container.firstElementChild!.lastElementChild).toHaveTextContent("050100150200250ms");
  });

  it("draws no axis without ticks", () => {
    const { container } = render(<LatencyBars rows={ROWS.slice(0, 1)} label="Upstream latency" ticks={[]} />);
    expect(container.firstElementChild!.children).toHaveLength(1);
  });
});

describe("latency geometry", () => {
  it("scales to a round maximum, averages, and marks probes over 1.5 × average as degraded", () => {
    const g = latencyGeometry([40, 44, 39, 47, 52, 41, 90, 45, 42], 120)!;
    expect(Math.round(g.avg)).toBe(49);
    expect(g.yTicks).toEqual([100, 50, 0]);
    expect(g.markers.filter((m) => m.degraded).map((m) => m.left)).toEqual([75]);
    expect(g.line.startsWith("M0.0,72.0")).toBe(true);
    expect(g.area.endsWith("L700,120 L0,120 Z")).toBe(true);
    expect(latencyGeometry([120, 310], 100)!.yTicks).toEqual([400, 300, 200, 100, 0]);
  });

  it("needs two finite points", () => {
    expect(latencyGeometry([42], 120)).toBeNull();
    expect(latencyGeometry([Number.NaN, 42], 120)).toBeNull();
  });
});

describe("LatencyChart", () => {
  it("draws the history with its average and rings degraded probes", () => {
    const { container } = render(<LatencyChart values={[40, 44, 39, 47, 52, 41, 90, 45, 42]} failed={false} dim={false} />);
    expect(screen.getByRole("img", { name: "Latency of the last 9 probes, average 49 ms" })).toBeInTheDocument();
    expect(container.querySelector("line[data-avg]")).not.toBeNull();
    expect(container.querySelectorAll('[data-marker="degraded"]')).toHaveLength(1);
    expect(container.querySelector('[data-marker="failed"]')).toBeNull();
    expect(screen.getByText("avg 49 ms")).toBeInTheDocument();
    expect(container.firstElementChild).not.toHaveAttribute("data-dim");
  });

  it("a failed current probe puts a rose × on the last point, and the chart dims", () => {
    const { container } = render(<LatencyChart values={[40, 44]} failed dim />);
    expect(container.querySelector('[data-marker="failed"]')).toHaveStyle({ left: "100%" });
    expect(container.firstElementChild).toHaveAttribute("data-dim", "true");
    expect(screen.getByRole("img")).toHaveAccessibleName("Latency of the last 2 probes, average 42 ms; the current probe failed");
  });

  it("says there is no history below two points", () => {
    render(<LatencyChart values={[42]} failed={false} dim={false} />);
    expect(screen.getByText("no probe history yet")).toBeInTheDocument();
    expect(screen.queryByRole("img")).toBeNull();
  });
});
