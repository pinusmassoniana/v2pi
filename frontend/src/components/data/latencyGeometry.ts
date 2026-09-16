/** viewBox width of the latency plot; the SVG stretches to its container. */
export const LATENCY_WIDTH = 700;
/** A probe slower than this multiple of the average is marked as degraded. */
export const DEGRADED_FACTOR = 1.5;

export interface LatencyGeometry {
  line: string;
  area: string;
  avg: number;
  /** The average line's y in viewBox units. */
  avgY: number;
  /** Y-axis labels in ms, top to bottom. */
  yTicks: number[];
  /** Each point's position in percent of the plot, and whether it is degraded. */
  markers: { left: number; top: number; degraded: boolean }[];
}

/** Scale, line, area, average and markers for a latency history; null below two points. */
export function latencyGeometry(values: readonly number[], height: number): LatencyGeometry | null {
  const points = values.filter((ms) => Number.isFinite(ms));
  if (points.length < 2) return null;
  const avg = points.reduce((sum, ms) => sum + ms, 0) / points.length;
  const max = Math.max(...points);
  const yMax = max <= 200 ? Math.max(50, Math.ceil(max / 50) * 50) : Math.ceil(max / 100) * 100;
  const step = yMax <= 200 ? 50 : yMax / 4;
  const x = (i: number) => (i / (points.length - 1)) * LATENCY_WIDTH;
  const y = (ms: number) => height - (ms / yMax) * height;
  const line = points.map((ms, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(ms).toFixed(1)}`).join(" ");
  const yTicks: number[] = [];
  for (let tick = yMax; tick >= 0; tick -= step) yTicks.push(tick);
  return {
    line,
    area: `${line} L${LATENCY_WIDTH},${height} L0,${height} Z`,
    avg,
    avgY: y(avg),
    yTicks,
    markers: points.map((ms, i) => ({ left: (x(i) / LATENCY_WIDTH) * 100, top: (y(ms) / height) * 100, degraded: ms > avg * DEGRADED_FACTOR })),
  };
}
