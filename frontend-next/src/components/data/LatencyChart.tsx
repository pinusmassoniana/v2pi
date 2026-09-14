import { useMemo } from "react";
import { cn } from "../../lib/cn";
import { LATENCY_WIDTH, latencyGeometry } from "./latencyGeometry";
import { useSvgId } from "./svgId";

export interface LatencyChartProps {
  /** Successful probe latencies, oldest first (the backend keeps the last 20). */
  values: readonly number[];
  /** The current probe failed: its point gets a rose ×. */
  failed: boolean;
  /** Stale or failing health: drawn, but dimmed. */
  dim: boolean;
  height?: number;
}

/** The active node's recent latency with its average; degraded probes ringed in amber. */
export function LatencyChart({ values, failed, dim, height = 120 }: LatencyChartProps) {
  const id = useSvgId("latency");
  // Rebuilding the path strings is the expensive part of a render; skip it unless the actual
  // inputs changed (mirrors trafficGeometry.ts's useMemo — see TrafficChart.tsx).
  const g = useMemo(() => latencyGeometry(values, height), [values, height]);
  if (!g) return <p className="grid place-items-center text-sm text-t3" style={{ height }}>no probe history yet</p>;
  const last = g.markers[g.markers.length - 1]!;
  const avg = Math.round(g.avg);
  return (
    <div data-dim={dim || undefined} className={cn("transition-opacity duration-200", dim && "opacity-45")}>
      <div className="flex gap-2">
        <div aria-hidden className="flex flex-col justify-between text-right text-[9.5px] leading-none text-t3" style={{ height }}>
          {g.yTicks.map((tick) => <span key={tick}>{tick}</span>)}
        </div>
        <div className="relative min-w-0 flex-1">
          <svg
            role="img"
            aria-label={`Latency of the last ${g.markers.length} probes, average ${avg} ms${failed ? "; the current probe failed" : ""}`}
            viewBox={`0 0 ${LATENCY_WIDTH} ${height}`}
            preserveAspectRatio="none"
            className="block w-full overflow-visible"
            style={{ height }}
          >
            <defs>
              <linearGradient id={id} gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2={height}>
                <stop offset="0" style={{ stopColor: "var(--g1)", stopOpacity: 0.25 }} />
                <stop offset="1" style={{ stopColor: "var(--g1)", stopOpacity: 0 }} />
              </linearGradient>
            </defs>
            <path d={g.area} style={{ fill: `url(#${id})` }} />
            <path d={g.line} fill="none" strokeWidth={2} strokeLinejoin="round" vectorEffect="non-scaling-stroke" style={{ stroke: "var(--g1)" }} />
            <line data-avg x1="0" x2={LATENCY_WIDTH} y1={g.avgY} y2={g.avgY} strokeDasharray="4 4" vectorEffect="non-scaling-stroke" style={{ stroke: "var(--g1)", strokeOpacity: 0.5 }} />
          </svg>
          {g.markers.map((m, i) =>
            m.degraded ? (
              <span key={i} data-marker="degraded" aria-hidden className="absolute size-2 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-warn" style={{ left: `${m.left}%`, top: `${m.top}%` }} />
            ) : null,
          )}
          {failed ? (
            <span data-marker="failed" aria-hidden className="absolute -translate-x-1/2 -translate-y-1/2 text-sm font-extrabold leading-none text-bad" style={{ left: `${last.left}%`, top: `${last.top}%` }}>×</span>
          ) : null}
        </div>
      </div>
      <p aria-hidden className="mt-2 flex flex-wrap gap-3.5 text-[11px] text-t2">
        <span className="inline-flex items-center gap-1.5"><i className="h-0.5 w-3 rounded-full bg-g1" />latency</span>
        <span className="inline-flex items-center gap-1.5"><i className="h-px w-3 bg-g1/50" />avg {avg} ms</span>
        <span className="inline-flex items-center gap-1.5"><i className="size-2 rounded-full border-2 border-warn" />degraded</span>
        <span className="text-bad">× probe failed</span>
      </p>
    </div>
  );
}
