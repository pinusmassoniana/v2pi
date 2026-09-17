import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { Events, Incident } from "../../api/client";
import { queries } from "../../api/keys";
import { CardHeader } from "../../components/data/CardHeader";
import { cardFallback, type CardQuery } from "../../components/data/CardState";
import { Chip } from "../../components/data/Chip";
import { Select } from "../../components/ui/Select";
import { GlassCard } from "../../components/ui/GlassCard";
import { cn } from "../../lib/cn";

export const WINDOWS = [
  { label: "24 h", sec: 86_400 },
  { label: "7 d", sec: 604_800 },
  { label: "30 d", sec: 2_592_000 },
] as const;

export const NO_INCIDENTS = "No drops in this window.";
export const INCIDENTS_NOTE =
  "A drop is the gap between the tunnel going down — disconnected, stopped, or out of nodes to fail over to — "
  + "and something bringing it back. Kept for 30 days.";

/** "4 m 12 s" / "1 h 06 m" / "12 s": short enough for a row, exact enough to compare two. */
export function durationText(seconds: number): string {
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))} s`;
  if (seconds < 3_600) return `${Math.floor(seconds / 60)} m ${String(Math.round(seconds % 60)).padStart(2, "0")} s`;
  return `${Math.floor(seconds / 3_600)} h ${String(Math.floor((seconds % 3_600) / 60)).padStart(2, "0")} m`;
}

/** "no downtime" reads better than "0 s" for the one figure people hope to see. */
export function downtimeText(seconds: number): string {
  return seconds > 0 ? durationText(seconds) : "no downtime";
}

/** The share of the window the tunnel was up, to one decimal — the number an SLA is read from. */
export function uptimeShare(downtimeSec: number, windowSec: number): string {
  if (windowSec <= 0) return "—";
  return `${(Math.max(0, 1 - downtimeSec / windowSec) * 100).toFixed(1)}%`;
}

function IncidentRow({ incident }: { incident: Incident }) {
  const started = new Date(incident.started * 1000);
  return (
    <li className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 py-1.5 text-[13px]">
      <span className="w-36 shrink-0 font-mono text-xs text-t2">
        {started.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}
      </span>
      <span className={cn("font-semibold", incident.ongoing ? "text-bad" : "text-t1")}>
        {durationText(incident.seconds)}{incident.ongoing ? " · still down" : ""}
      </span>
      <span className="min-w-0 flex-1 truncate text-t3">{incident.detail || incident.kind}</span>
    </li>
  );
}

/**
 * A9: how often the tunnel dropped and for how long. The event log used to be a 40-entry ring in
 * the settings k/v — enough to explain the last few minutes, never enough to answer this.
 */
export function IncidentsCard({ className }: { className?: string }) {
  const [windowSec, setWindowSec] = useState<number>(604_800);
  const events = useQuery(queries.events(windowSec));
  const data: Events | undefined = events.data;
  return (
    <GlassCard aria-label="Incidents" className={className}>
      <CardHeader
        title="Incidents"
        detail={data ? `${data.incidents.length} in this window` : "tunnel drops"}
        aside={
          <Select aria-label="Incident window" value={String(windowSec)} onChange={(event) => setWindowSec(Number(event.target.value))} className="h-8 w-24 text-xs">
            {WINDOWS.map((option) => <option key={option.sec} value={option.sec}>{option.label}</option>)}
          </Select>
        }
      />
      {cardFallback([events as CardQuery], "incidents did not load", "h-32") ?? (
        <>
          <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
            <p className="text-[15px] font-bold tracking-tight text-t1">
              {downtimeText(data!.downtime_sec)}
              <span className="ml-1.5 text-[11px] font-normal text-t3">total</span>
            </p>
            <p className="text-[15px] font-bold tracking-tight text-t1">
              {uptimeShare(data!.downtime_sec, data!.window_sec)}
              <span className="ml-1.5 text-[11px] font-normal text-t3">up</span>
            </p>
            {data!.incidents.some((incident) => incident.ongoing) ? <Chip tone="bad">down now</Chip> : null}
          </div>
          {data!.incidents.length === 0 ? (
            <p className="mt-2 text-sm text-t3">{NO_INCIDENTS}</p>
          ) : (
            <ul aria-label="Drops" className="mt-1.5 flex flex-col divide-y divide-line">
              {data!.incidents.slice(0, 8).map((incident) => (
                <IncidentRow key={`${incident.started}-${incident.kind}`} incident={incident} />
              ))}
            </ul>
          )}
          <p className="mt-2 text-[11px] leading-relaxed text-t3">{INCIDENTS_NOTE}</p>
        </>
      )}
    </GlassCard>
  );
}
