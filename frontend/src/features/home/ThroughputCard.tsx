import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useMemo, useState, type ReactNode } from "react";
import { queries } from "../../api/keys";
import { TrafficChart, type TrafficWindowSec } from "../../components/data/TrafficChart";
import { CardHeader } from "../../components/data/CardHeader";
import { GlassCard } from "../../components/ui/GlassCard";
import { EmptyState, ErrorState, Skeleton } from "../../components/ui/States";
import { chartStale, peakOf, probeFor } from "./derive";
import { useTrafficSeries, type TrafficSeries } from "./useTrafficSeries";

export interface ThroughputCardProps {
  windowSec: TrafficWindowSec;
  onWindowChange: (windowSec: TrafficWindowSec) => void;
  series: TrafficSeries;
  /** Only a probe of this node says whether tunnel health is fresh. */
  activeNodeId: number | null;
  className?: string;
}

/** O6 / H2: the throughput chart in its card, with the stats-off, loading and error states in place of the plot. */
export function ThroughputCard({ windowSec, onWindowChange, series, activeNodeId, className }: ThroughputCardProps) {
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
      {series.liveHistoryFailed ? (
        <ErrorState role="status" message="Traffic history did not load — showing live samples only" onRetry={series.refillHistory} />
      ) : null}
      <TrafficChart
        samples={series.samples}
        windowSec={windowSec}
        onWindowChange={onWindowChange}
        peak={peak}
        stale={chartStale(windowSec, activeNodeId, probeFor(series.live, activeNodeId))}
        body={body}
      />
    </GlassCard>
  );
}

/** Overview's chart owns its window: switching it re-renders this card, not the page. */
export function OverviewThroughput({ className }: { className?: string }) {
  const [windowSec, setWindowSec] = useState<TrafficWindowSec>(600);
  const series = useTrafficSeries(windowSec);
  const activeNodeId = useQuery(queries.status()).data?.active_node_id ?? null;
  return <ThroughputCard windowSec={windowSec} onWindowChange={setWindowSec} series={series} activeNodeId={activeNodeId} className={className} />;
}
