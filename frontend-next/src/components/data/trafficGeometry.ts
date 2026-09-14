import type { TrafficSample } from "../../api/traffic";

/** viewBox width of the plot; the SVG stretches to its container, strokes do not. */
export const CHART_WIDTH = 700;
/** More points than this cannot be told apart at chart width, so the series is thinned to it. */
export const MAX_POINTS = 700;

/** The samples inside the window that ends at the newest sample. `samples` must be oldest first. */
export function windowSlice(samples: readonly TrafficSample[], windowSec: number): TrafficSample[] {
  if (samples.length === 0) return [];
  const start = samples[samples.length - 1]!.ts - windowSec * 1000;
  let first = samples.length - 1;
  while (first > 0 && samples[first - 1]!.ts >= start) first--;
  return samples.slice(first);
}

/** Evenly thinned to at most `max` items, always keeping the newest. */
export function downsample<T>(items: readonly T[], max: number): T[] {
  if (items.length <= max) return items.slice();
  const step = items.length / max;
  const out: T[] = [];
  for (let i = 0; i < max; i++) out.push(items[Math.floor(i * step)]!);
  out[out.length - 1] = items[items.length - 1]!;
  return out;
}

/** The next 1 / 2 / 5 × 10ⁿ at or above `value`, so the axis reads in round numbers. */
export function niceMax(value: number): number {
  if (!(value > 0)) return 1;
  const power = 10 ** Math.floor(Math.log10(value));
  const f = value / power;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * power;
}

/** Axis label for a rate in bits per second: "20M", "500k", "0". */
export function axisRate(bps: number): string {
  if (bps >= 1e6) return `${Number((bps / 1e6).toFixed(1))}M`;
  if (bps >= 1e3) return `${Math.round(bps / 1e3)}k`;
  return String(Math.round(bps));
}

/** "now", "-30s", "-5m", "-6h", "-3d" for a point `secondsAgo` before the newest sample. */
export function relativeLabel(secondsAgo: number, windowSec: number): string {
  const s = Math.round(secondsAgo);
  if (s <= 0) return "now";
  if (windowSec <= 120) return `-${s}s`;
  const m = Math.round(s / 60);
  if (m < 60) return `-${m}m`;
  const h = Math.round(m / 60);
  return h >= 48 ? `-${Math.round(h / 24)}d` : `-${h}h`;
}

export interface TrafficGeometry {
  lineDown: string;
  areaDown: string;
  lineUp: string;
  /** Gridline y positions in viewBox units, top to bottom. */
  gridY: number[];
  /** Y-axis labels, top to bottom. */
  yTicks: string[];
  /** X-axis labels, left to right. */
  xTicks: string[];
  /** Points drawn after thinning. */
  points: number;
  /** Where the peak marker sits, in percent of the plot. */
  peak: { left: number; top: number } | null;
}

/** Everything the throughput chart draws, computed in one pass over the window's samples. */
export function trafficGeometry(
  samples: readonly TrafficSample[], windowSec: number, peak: { bps: number; ts: number } | null, height: number,
): TrafficGeometry {
  const end = samples.length ? samples[samples.length - 1]!.ts : 0;
  const span = windowSec * 1000;
  const start = end - span;
  let highest = 0;
  for (const sample of samples) highest = Math.max(highest, sample.up, sample.down);
  const yMax = niceMax(highest);
  const x = (ts: number) => Math.min(1, Math.max(0, (ts - start) / span)) * CHART_WIDTH;
  const y = (bps: number) => height - (bps / yMax) * height;
  const drawn = downsample(samples, MAX_POINTS);
  const line = (key: "up" | "down") =>
    drawn.length < 2 ? "" : drawn.map((s, i) => `${i ? "L" : "M"}${x(s.ts).toFixed(1)},${y(s[key]).toFixed(1)}`).join(" ");
  const lineDown = line("down");
  const areaDown = lineDown
    ? `${lineDown} L${x(drawn[drawn.length - 1]!.ts).toFixed(1)},${height} L${x(drawn[0]!.ts).toFixed(1)},${height} Z`
    : "";
  const fractions = [1, 0.75, 0.5, 0.25, 0];
  return {
    lineDown,
    areaDown,
    lineUp: line("up"),
    gridY: fractions.map((f) => y(yMax * f)),
    yTicks: fractions.map((f) => axisRate(yMax * f)),
    xTicks: [0, 0.25, 0.5, 0.75, 1].map((f) => relativeLabel(((1 - f) * span) / 1000, windowSec)),
    points: drawn.length,
    peak: peak && drawn.length >= 2 ? { left: (x(peak.ts) / CHART_WIDTH) * 100, top: (y(peak.bps) / height) * 100 } : null,
  };
}
