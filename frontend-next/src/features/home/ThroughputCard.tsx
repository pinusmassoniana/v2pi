import { Link } from "@tanstack/react-router";
import { useMemo, useState, type ReactNode } from "react";
import { TrafficChart, type TrafficWindowSec } from "../../components/data/TrafficChart";
import { CardHeader } from "../../components/data/CardHeader";
import { GlassCard } from "../../components/ui/GlassCard";
import { EmptyState, ErrorState, Skeleton } from "../../components/ui/States";
import { peakOf } from "./derive";
import { useTrafficSeries, type TrafficSeries } from "./useTrafficSeries";

export interface ThroughputCardProps {
  windowSec: TrafficWindowSec;
  onWindowChange: (windowSec: TrafficWindowSec) => void;
  series: TrafficSeries;
  className?: string;
}

/** O6 / H2: the throughput chart in its card, with the stats-off, loading and error states in place of the plot. */
export function ThroughputCard({ windowSec, onWindowChange, series, className }: ThroughputCardProps) {
  // Referentially stable across renders where the samples haven't changed, so TrafficChart's own
  // geometry memo (keyed on this prop) doesn't rebuild on every parent render — see the carry-over note.
  const peak = useMemo(() => (series.disabled ? null : peakOf(series.samples)), [series.disabled, series.samples]);
  let body: ReactNode = null;
  if (series.disabled) {
    body = (
      <EmptyState title="Traffic stats are off">
        Turn them on in <Link to="/system/panel" className="font-semibold text-t1 underline">System › Panel</Link>.
      </EmptyState>
    );
  } else if (series.error) {
    body = <ErrorState message="Traffic history did not load" onRetry={series.retry} />;
  } else if (series.pending) {
    body = <Skeleton className="h-[204px]" />;
  }
  return (
    <GlassCard aria-label="Throughput" className={className}>
      <CardHeader title="Throughput" />
      <TrafficChart
        samples={series.samples}
        windowSec={windowSec}
        onWindowChange={onWindowChange}
        peak={peak}
        stale={series.live?.active?.stale !== false}
        body={body}
      />
    </GlassCard>
  );
}

/** Overview's chart owns its window: switching it re-renders this card, not the page. */
export function OverviewThroughput({ className }: { className?: string }) {
  const [windowSec, setWindowSec] = useState<TrafficWindowSec>(600);
  const series = useTrafficSeries(windowSec);
  return <ThroughputCard windowSec={windowSec} onWindowChange={setWindowSec} series={series} className={className} />;
}
