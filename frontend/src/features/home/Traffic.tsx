import { useQuery } from "@tanstack/react-query";
import { useMemo, useState, type ReactNode } from "react";
import type { ConnEvent, Node, NodeHealth, Status } from "../../api/client";
import { serverNow } from "../../api/clock";
import { queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { useTraffic } from "../../api/traffic";
import { CardHeader } from "../../components/data/CardHeader";
import { Kpi } from "../../components/data/Kpi";
import { LatencyBars } from "../../components/data/LatencyBars";
import { LatencyChart } from "../../components/data/LatencyChart";
import { windowLabel, type TrafficWindowSec } from "../../components/data/TrafficChart";
import { Uptime } from "../../components/data/Uptime";
import { GlassCard } from "../../components/ui/GlassCard";
import { fmtRate, splitUnit } from "../../lib/format";
import { NETWORK_POLL_MS, SLOW_POLL_MS } from "../../api/cadence";
import { cardFallback, staleNotice, type CardQuery } from "../../components/data/CardState";
import {
  activeFlag, activeNode, activeRow, clockTime, failoverHistory, latencyStats, peakOf, probeFor, sinceLabel, standbyRows,
  tunnelLabel, whenLabel,
} from "./derive";
import { ThroughputCard } from "./ThroughputCard";
import { UsageCard } from "./UsageCard";
import { useRefreshOnSwitch } from "./useRefreshOnSwitch";
import { useTrafficSeries } from "./useTrafficSeries";

const H3_TICKS = [0, 50, 100, 150, 200, 250];

function FailoverRows({ events, nowMs }: { events: readonly ConnEvent[]; nowMs: number }) {
  if (events.length === 0) return <p className="py-2 text-sm text-t3">No failovers recorded.</p>;
  return (
    <>
      <table className="hidden w-full table-fixed text-left text-xs md:table">
        <thead>
          <tr className="text-[9.5px] uppercase tracking-[.07em] text-t3">
            <th scope="col" className="w-44 pb-1.5 font-semibold">When</th>
            <th scope="col" className="w-32 pb-1.5 font-semibold">Kind</th>
            <th scope="col" className="pb-1.5 font-semibold">Detail</th>
          </tr>
        </thead>
        <tbody>
          {events.map((event, index) => (
            <tr key={`${event.ts}-${index}`} className="border-t border-line">
              <td className="py-2 font-mono text-t3">{whenLabel(event.ts, nowMs)}</td>
              <td className="py-2 text-t1">{event.kind}</td>
              <td className="truncate py-2 text-t2">{event.detail}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <ul aria-label="Failovers" className="flex flex-col md:hidden">
        {events.map((event, index) => (
          <li key={`${event.ts}-${index}`} className="grid grid-cols-[5.5rem_minmax(0,1fr)] gap-2.5 border-t border-line py-2 text-xs first:border-t-0">
            <span className="font-mono text-t3">{whenLabel(event.ts, nowMs)}</span>
            <span className="min-w-0 text-t1">
              {event.kind}
              <span className="block truncate text-[11px] text-t2">{event.detail}</span>
            </span>
          </li>
        ))}
      </ul>
    </>
  );
}

/**
 * The live probe of the active node, from the store — null while stats are off or no frame is about that node —
 * and whether it stopped updating (`stale`: shown dimmed, but deciding nothing).
 */
function useActiveProbe(activeNodeId: number | null) {
  const traffic = useTraffic();
  return { probe: probeFor(traffic.disabled ? null : traffic.live, activeNodeId), stale: !traffic.disabled && !traffic.fresh };
}

/**
 * The window switcher's state, its series and the two things drawn from it — the peak KPI and the throughput
 * chart. A live frame re-renders this and the cards that show live values, never the page: `children` (the KPIs
 * between the two) are the page's elements, unchanged when only this re-renders.
 */
function TrafficTop({ activeNodeId, children }: { activeNodeId: number | null; children: ReactNode }) {
  const [windowSec, setWindowSec] = useState<TrafficWindowSec>(600);
  const series = useTrafficSeries(windowSec);
  const peak = useMemo(() => (series.disabled ? null : peakOf(series.samples)), [series.disabled, series.samples]);
  const peakValue = peak ? splitUnit(fmtRate(peak.bps)) : { value: "—", unit: "" };
  let peakSub: ReactNode = "no traffic in this window";
  if (peak) peakSub = <>at <span className="font-mono text-t2">{clockTime(peak.ts / 1000)}</span></>;
  else if (!series.disabled && (series.pending || series.error)) peakSub = "—";   // the recorded window has not loaded
  return (
    <>
      <Kpi label={`Peak download · ${windowLabel(windowSec)}`} value={peakValue.value} unit={peakValue.unit} sub={peakSub} className="xl:col-span-3" />
      {children}
      <ThroughputCard windowSec={windowSec} onWindowChange={setWindowSec} series={series} activeNodeId={activeNodeId} className="md:col-span-2 xl:col-span-12" />
    </>
  );
}

function ActiveLatencyKpi({ activeNodeId }: { activeNodeId: number | null }) {
  const { probe, stale } = useActiveProbe(activeNodeId);
  const latency = latencyStats(probe, serverNow());
  return (
    <Kpi
      label="Active latency"
      value={latency.ms ?? "—"}
      unit={latency.ms !== null ? "ms" : undefined}
      sub={<span className={latency.fresh ? undefined : probe?.stale ? "text-warn" : undefined}>{latency.sub}</span>}
      dim={stale}
      className="xl:col-span-3"
    />
  );
}

function UptimeKpi({ status, statusError }: { status: Status | undefined; statusError: boolean }) {
  const { probe, stale } = useActiveProbe(status?.active_node_id ?? null);
  const tunnel = tunnelLabel(status, statusError, stale ? null : probe);
  const online = tunnel.label === "ONLINE";
  const since = status?.active_since ?? null;
  let sub: ReactNode = tunnel.label === "UNKNOWN" ? "unknown" : "not connected";
  if (online && since !== null) sub = <>since <span className="font-mono text-t2">{sinceLabel(since)}</span></>;
  return <Kpi label="Uptime" value={<Uptime since={since} running={online} coarse />} sub={sub} className="xl:col-span-3" />;
}

function LatencyBarsCard({ activeNodeId, nodes, health }: {
  activeNodeId: number | null;
  nodes: CardQuery & { data: Node[] | undefined };
  health: CardQuery & { data: NodeHealth[] | undefined };
}) {
  const { probe, stale } = useActiveProbe(activeNodeId);
  const fallback = cardFallback([nodes, health], "Node health did not load", "h-48");
  const rows = [
    activeRow(activeNode(nodes.data, activeNodeId), probe, stale),
    ...standbyRows(nodes.data ?? [], health.data ?? [], activeNodeId, serverNow(), Infinity),
  ].filter((row) => row !== null);
  return (
    <GlassCard aria-label="Probe latency by node" aria-busy={(fallback !== null && !nodes.isError && !health.isError) || undefined} className="md:col-span-2 xl:col-span-6">
      <CardHeader
        title="Probe latency by node"
        aside={<span className="inline-flex items-center gap-1.5 text-[11px] text-t2"><i aria-hidden className="h-2.5 w-px bg-warn/70" />slow &gt; 150 ms</span>}
      />
      {fallback ?? (
        <>
          {staleNotice([nodes, health], "Node health did not refresh")}
          {rows.length === 0 ? <p className="py-2 text-sm text-t3">No nodes yet.</p> : <LatencyBars rows={rows} label="Nodes by latency" ticks={H3_TICKS} thresholdLine />}
        </>
      )}
    </GlassCard>
  );
}

function ActiveLatencyCard({ activeNodeId, active }: { activeNodeId: number | null; active: Node | undefined }) {
  const { probe } = useActiveProbe(activeNodeId);
  // Every frame carries a new lat_history array, even when no probe ran since the last one. Key the chart's input
  // on what the history holds, so LatencyChart's geometry memo rebuilds only when a probe actually changed it.
  const historyKey = probe?.lat_history.join(",") ?? "";
  const history = useMemo(() => (historyKey ? historyKey.split(",").map(Number) : []), [historyKey]);
  const flag = activeFlag(probe);
  return (
    <GlassCard aria-label="Active node latency" className="md:col-span-2 xl:col-span-6">
      <CardHeader
        title="Active node latency"
        detail={active ? `${flag ? `${flag} ` : ""}${active.name}${history.length ? ` · last ${history.length} probes` : ""}` : undefined}
      />
      <LatencyChart values={history} failed={probe?.real_ok === false} dim={probe?.stale !== false || probe?.real_ok === false} />
    </GlassCard>
  );
}

/**
 * Home › Traffic: trends rather than "right now". Polls network and node health (the same cadence Overview
 * uses — only one of the two pages is ever mounted); node names are read once when it opens. The page reads no
 * live traffic itself, so a frame re-renders only the cards that show it.
 */
export function Traffic() {
  const status = useQuery(queries.status());
  const network = usePolledQuery(queries.network(), NETWORK_POLL_MS);
  const health = usePolledQuery(queries.nodeHealth(), SLOW_POLL_MS);
  const nodes = useQuery(queries.nodes());
  useRefreshOnSwitch(status.data);

  const nowMs = serverNow();
  const activeId = status.data?.active_node_id ?? null;
  const history = network.data ? failoverHistory(network.data.events, status.data?.last_failover_at) : [];
  // null: the gateway could not count them (it logs why) — "—", never a reassuring 0.
  const failovers = status.data?.failovers_24h === null ? null : (status.data?.failovers_24h ?? 0);
  // "last …" under the count: the newest event the count itself counts (kind exactly "failover", as the backend's
  // failovers_24h does) — a manual switch is not a failover.
  const lastFailover = network.data
    ? failoverHistory(network.data.events.filter((event) => event.kind === "failover"), status.data?.last_failover_at, 1)[0]
    : undefined;
  const historyFallback = cardFallback([network], "Failover history did not load", "h-32");

  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-12">
      <TrafficTop activeNodeId={activeId}>
        <ActiveLatencyKpi activeNodeId={activeId} />
        <Kpi
          label="Failovers · 24h"
          value={failovers === null ? "—" : String(failovers)}
          tone={failovers ? "warn" : "neutral"}
          sub={lastFailover ? <>last <span className="font-mono text-t2">{whenLabel(lastFailover.ts, nowMs)}</span></> : "none recorded"}
          className="xl:col-span-3"
        />
        <UptimeKpi status={status.data} statusError={status.isError} />
      </TrafficTop>

      <UsageCard className="md:col-span-2 xl:col-span-12" />
      <LatencyBarsCard activeNodeId={activeId} nodes={nodes} health={health} />
      <ActiveLatencyCard activeNodeId={activeId} active={activeNode(nodes.data, activeId)} />

      <GlassCard aria-label="Failover history" aria-busy={(historyFallback !== null && !network.isError) || undefined} className="md:col-span-2 xl:col-span-12">
        <CardHeader title="Failover history" detail="last 8" />
        {historyFallback ?? (
          <>
            {staleNotice([network], "Failover history did not refresh")}
            <FailoverRows events={history} nowMs={nowMs} />
          </>
        )}
      </GlassCard>
    </div>
  );
}
