import { useMemo, type ReactNode } from "react";
import type { TrafficSample } from "../../api/traffic";
import { cn } from "../../lib/cn";
import { fmtRate } from "../../lib/format";
import { useSvgId } from "./svgId";
import { CHART_WIDTH, trafficGeometry } from "./trafficGeometry";

export const TRAFFIC_WINDOWS = [
  { label: "1m", sec: 60 },
  { label: "10m", sec: 600 },
  { label: "1h", sec: 3_600 },
  { label: "24h", sec: 86_400 },
  { label: "7d", sec: 604_800 },
] as const;

export type TrafficWindowSec = (typeof TRAFFIC_WINDOWS)[number]["sec"];

export function windowLabel(windowSec: TrafficWindowSec): string {
  return TRAFFIC_WINDOWS.find((w) => w.sec === windowSec)?.label ?? `${windowSec}s`;
}

export interface TrafficChartProps {
  /** The selected window's samples, oldest first (ts in ms). */
  samples: readonly TrafficSample[];
  windowSec: TrafficWindowSec;
  onWindowChange: (windowSec: TrafficWindowSec) => void;
  /** The window's download peak, marked on the plot. */
  peak: { bps: number; ts: number } | null;
  /** Tunnel health is not known to be fresh: rates are shown dimmed, with a caption. */
  stale: boolean;
  /** Replaces the plot: a skeleton, an error, or the stats-off state. */
  body?: ReactNode;
  height?: number;
}

const pad = (n: number) => String(n).padStart(2, "0");
const clock = (ms: number) => {
  const d = new Date(ms);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
};

export function TrafficChart({ samples, windowSec, onWindowChange, peak, stale, body, height = 184 }: TrafficChartProps) {
  const id = useSvgId("traffic");
  // Rebuilding two 700-point path strings is the expensive part of a render; skip it unless the
  // window's actual inputs changed (a 24h/7d window's `samples` stays referentially stable between
  // history polls even while live frames keep arriving — see useTrafficSeries.ts).
  const g = useMemo(() => trafficGeometry(samples, windowSec, peak, height), [samples, windowSec, peak, height]);
  const label = windowLabel(windowSec);

  let plot: ReactNode = body;
  if (plot === undefined || plot === null) {
    plot = g.points < 2 ? (
      <p className="grid place-items-center text-sm text-t3" style={{ height }}>Waiting for traffic…</p>
    ) : (
      <div data-stale={stale || undefined} className={cn("flex gap-2 transition-[opacity,filter] duration-200", stale && "opacity-40 saturate-[.2]")}>
        <div aria-hidden className="flex flex-col justify-between pb-5 text-right text-[9.5px] leading-none text-t3" style={{ height: height + 20 }}>
          {g.yTicks.map((tick, i) => <span key={i}>{tick}</span>)}
        </div>
        <div className="min-w-0 flex-1">
          <div className="relative">
            <svg
              role="img"
              aria-label={`Throughput over the last ${label}${stale ? "; tunnel health is stale" : ""}`}
              viewBox={`0 0 ${CHART_WIDTH} ${height}`}
              preserveAspectRatio="none"
              className="block w-full overflow-visible"
              style={{ height }}
            >
              <defs>
                <linearGradient id={`${id}-line`} gradientUnits="userSpaceOnUse" x1="0" y1="0" x2={CHART_WIDTH} y2="0">
                  <stop offset="0" style={{ stopColor: "var(--g1)" }} />
                  <stop offset="1" style={{ stopColor: "var(--g2)" }} />
                </linearGradient>
                <linearGradient id={`${id}-fill`} gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2={height}>
                  <stop offset="0" style={{ stopColor: "var(--g2)", stopOpacity: 0.3 }} />
                  <stop offset="1" style={{ stopColor: "var(--g2)", stopOpacity: 0 }} />
                </linearGradient>
              </defs>
              {g.gridY.map((y, i) => (
                <line key={i} x1="0" x2={CHART_WIDTH} y1={y} y2={y} vectorEffect="non-scaling-stroke" style={{ stroke: "var(--grid)" }} />
              ))}
              <path d={g.areaDown} style={{ fill: `url(#${id}-fill)` }} />
              <path data-series="down" d={g.lineDown} fill="none" strokeWidth={2} strokeLinejoin="round" vectorEffect="non-scaling-stroke" style={{ stroke: `url(#${id}-line)` }} />
              <path data-series="up" d={g.lineUp} fill="none" strokeWidth={1.5} strokeLinejoin="round" vectorEffect="non-scaling-stroke" style={{ stroke: "var(--series-up)" }} />
            </svg>
            {peak && g.peak ? (
              <>
                <span aria-hidden className="absolute size-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-t1 shadow-[0_0_0_3px_var(--g2)]" style={{ left: `${g.peak.left}%`, top: `${g.peak.top}%` }} />
                <span
                  data-peak
                  className={cn(
                    "absolute -translate-y-1/2 whitespace-nowrap rounded-md border border-line bg-solid px-2 py-0.5 text-[10.5px] font-semibold text-t1",
                    g.peak.left > 50 ? "-translate-x-[calc(100%+12px)]" : "translate-x-3",
                  )}
                  style={{ left: `${g.peak.left}%`, top: `${Math.max(8, g.peak.top)}%` }}
                >
                  peak {fmtRate(peak.bps)} · <span className="font-mono">{clock(peak.ts)}</span>
                </span>
              </>
            ) : null}
          </div>
          <div aria-hidden className="mt-1.5 flex justify-between font-mono text-[9.5px] leading-none text-t3">
            {g.xTicks.map((tick, i) => <span key={i}>{tick}</span>)}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div aria-hidden className="flex items-center gap-3 text-[11px] text-t2">
          <span className="inline-flex items-center gap-1.5"><i className="h-0.5 w-3.5 rounded-full bg-linear-to-r from-g1 to-g2" />down</span>
          <span className="inline-flex items-center gap-1.5"><i className="h-0.5 w-3.5 rounded-full bg-series-up" />up</span>
        </div>
        <div role="group" aria-label="Chart window" className="inline-flex gap-0.5 rounded-[10px] border border-line bg-glass p-[3px]">
          {TRAFFIC_WINDOWS.map((w) => (
            <button
              key={w.sec}
              type="button"
              aria-pressed={w.sec === windowSec}
              onClick={() => onWindowChange(w.sec)}
              className={cn(
                "rounded-[7px] px-2.5 py-1 text-[10.5px] font-semibold text-t3 transition-colors hover:text-t1 focus-visible:outline-2 focus-visible:outline-g2",
                w.sec === windowSec && "bg-brand text-bg hover:text-bg",
              )}
            >
              {w.label}
            </button>
          ))}
        </div>
      </div>
      {plot}
      {stale && !body ? (
        <p role="status" className="flex items-center gap-2 text-[11.5px] text-warn">
          <span aria-hidden className="grid size-5 place-items-center rounded-md bg-warn/20 text-[11px] font-extrabold">!</span>
          Tunnel health is stale — rates are not proof of a healthy tunnel
        </p>
      ) : null}
    </div>
  );
}
