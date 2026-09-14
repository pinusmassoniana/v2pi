import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { ConnEvent } from "../../api/client";
import { serverNow } from "../../api/clock";
import { queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { CardHeader } from "../../components/data/CardHeader";
import { Kpi } from "../../components/data/Kpi";
import { LatencyBars } from "../../components/data/LatencyBars";
import { LatencyChart } from "../../components/data/LatencyChart";
import { windowLabel, type TrafficWindowSec } from "../../components/data/TrafficChart";
import { Uptime } from "../../components/data/Uptime";
import { GlassCard } from "../../components/ui/GlassCard";
import { fmtRate, splitUnit } from "../../lib/format";
import { NETWORK_POLL_MS, SLOW_POLL_MS } from "./cadence";
import { cardFallback, staleNotice } from "./CardState";
import {
  activeFlag, activeNode, activeRow, clockTime, failoverHistory, latencyStats, peakOf, sinceLabel, standbyRows,
  tunnelLabel, whenLabel,
} from "./derive";
import { ThroughputCard } from "./ThroughputCard";
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
 * Home › Traffic: trends rather than "right now". Polls network and node health (the same cadence Overview
 * uses — only one of the two pages is ever mounted); node names are read once when it opens.
 */
export function Traffic() {
  const status = useQuery(queries.status());
  const network = usePolledQuery(queries.network(), NETWORK_POLL_MS);
  const health = usePolledQuery(queries.nodeHealth(), SLOW_POLL_MS);
  const nodes = useQuery(queries.nodes());
  const [windowSec, setWindowSec] = useState<TrafficWindowSec>(600);
  const series = useTrafficSeries(windowSec);

  const nowMs = serverNow();
  const probe = series.live?.active ?? null;
  const activeId = status.data?.active_node_id ?? null;
  const active = activeNode(nodes.data, activeId);
  const online = tunnelLabel(status.data, status.isError).label === "ONLINE";
  const peak = series.disabled ? null : peakOf(series.samples);
  const peakValue = peak ? splitUnit(fmtRate(peak.bps)) : { value: "—", unit: "" };
  const latency = latencyStats(probe, nowMs);
  const history = network.data ? failoverHistory(network.data.events, status.data?.last_failover_at) : [];
  const failovers = network.data?.status.failovers_24h ?? status.data?.failovers_24h ?? 0;
  const since = status.data?.active_since ?? null;

  const barsFallback = cardFallback([nodes, health], "Node health did not load", "h-48");
  const rows = [
    activeRow(active, probe),
    ...standbyRows(nodes.data ?? [], health.data ?? [], activeId, nowMs, Infinity),
  ].filter((row) => row !== null);
  const probeHistory = probe && probe.node_id === activeId ? probe.lat_history : [];
  const flag = activeFlag(activeId, probe);
  const historyFallback = cardFallback([network], "Failover history did not load", "h-32");

  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-12">
      <Kpi
        label={`Peak download · ${windowLabel(windowSec)}`}
        value={peakValue.value}
        unit={peakValue.unit}
        sub={peak ? <>at <span className="font-mono text-t2">{clockTime(peak.ts / 1000)}</span></> : "no traffic in this window"}
        className="xl:col-span-3"
      />
      <Kpi
        label="Active latency"
        value={latency.ms ?? "—"}
        unit={latency.ms !== null ? "ms" : undefined}
        sub={<span className={latency.fresh ? undefined : probe?.stale ? "text-warn" : undefined}>{latency.sub}</span>}
        className="xl:col-span-3"
      />
      <Kpi
        label="Failovers · 24h"
        value={String(failovers)}
        tone={failovers > 0 ? "warn" : "neutral"}
        sub={history[0] ? <>last <span className="font-mono text-t2">{whenLabel(history[0].ts, nowMs)}</span></> : "none recorded"}
        className="xl:col-span-3"
      />
      <Kpi
        label="Uptime"
        value={<Uptime since={since} running={online} coarse />}
        sub={online && since !== null ? <>since <span className="font-mono text-t2">{sinceLabel(since)}</span></> : "not connected"}
        className="xl:col-span-3"
      />

      <ThroughputCard windowSec={windowSec} onWindowChange={setWindowSec} series={series} className="md:col-span-2 xl:col-span-12" />

      <GlassCard aria-label="Probe latency by node" aria-busy={(barsFallback !== null && !nodes.isError && !health.isError) || undefined} className="md:col-span-2 xl:col-span-6">
        <CardHeader
          title="Probe latency by node"
          aside={<span className="inline-flex items-center gap-1.5 text-[11px] text-t2"><i aria-hidden className="h-2.5 w-px bg-warn/70" />slow &gt; 150 ms</span>}
        />
        {barsFallback ?? (
          <>
            {staleNotice([nodes, health], "Node health did not refresh")}
            {rows.length === 0 ? <p className="py-2 text-sm text-t3">No nodes yet.</p> : <LatencyBars rows={rows} label="Nodes by latency" ticks={H3_TICKS} thresholdLine />}
          </>
        )}
      </GlassCard>

      <GlassCard aria-label="Active node latency" className="md:col-span-2 xl:col-span-6">
        <CardHeader
          title="Active node latency"
          detail={active ? `${flag ? `${flag} ` : ""}${active.name}${probeHistory.length ? ` · last ${probeHistory.length} probes` : ""}` : undefined}
        />
        <LatencyChart values={probeHistory} failed={probe?.real_ok === false} dim={probe?.stale !== false || probe?.real_ok === false} />
      </GlassCard>

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
