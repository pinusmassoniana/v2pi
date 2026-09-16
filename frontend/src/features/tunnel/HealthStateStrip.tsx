import { useQuery } from "@tanstack/react-query";
import type { Status } from "../../api/client";
import { queries } from "../../api/keys";
import { Ago } from "../../components/data/Ago";
import { CardHeader } from "../../components/data/CardHeader";
import { KeyValueRows } from "../../components/data/KeyValueRows";
import { GlassCard } from "../../components/ui/GlassCard";
import { cn } from "../../lib/cn";

/** The status fields the strip reads; selecting them keeps the strip from re-rendering on every other status change. */
export function healthState(status: Status) {
  return {
    healthEnabled: status.health_enabled,
    failoverEnabled: status.failover_enabled,
    failoverReady: status.failover_ready,
    standbys: status.eligible_standby_count,
    failovers24h: status.failovers_24h,
    lastFailoverAt: status.last_failover_at,
  };
}

/** What the strip shows for the auto-failover tile. */
export function failoverLabel(state: ReturnType<typeof healthState>): string {
  if (state.failoverEnabled === false) return "Off";
  // The backend's failover_ready also requires health checks on; name that cause instead of "No eligible standby".
  if (state.healthEnabled === false) return "Needs health checks";
  return state.failoverReady ? "Failover ready" : "No eligible standby";
}

/** G3 / G4 read-only: health checks on or off, failover readiness, failovers in 24 h and the last switch, from the status poll. */
export function HealthStateStrip({ className }: { className?: string }) {
  // The shell polls status; this reads the cache through a select.
  const status = useQuery({ ...queries.status(), select: healthState });
  const state = status.isError ? undefined : status.data;
  const unknown = "unknown";
  const rows = [
    {
      key: "Health checks",
      value: state ? <span className={state.healthEnabled ? "text-ok" : "text-t2"}>{state.healthEnabled ? "On" : "Off"}</span> : unknown,
    },
    {
      key: "Auto-failover",
      value: state ? <span className={cn(state.failoverEnabled !== false && state.failoverReady ? "text-ok" : "text-warn")}>{failoverLabel(state)}</span> : unknown,
      sub: state ? `${state.standbys ?? 0} eligible standby` : undefined,
    },
    { key: "Failovers · 24 h", value: state ? (state.failovers24h ?? "—") : unknown },
    {
      key: "Last switch",
      value: state ? (state.lastFailoverAt ? <Ago at={new Date(state.lastFailoverAt * 1000).toISOString()} /> : "never") : unknown,
    },
  ];
  return (
    <GlassCard aria-label="Current state" className={className}>
      <CardHeader title="Current state" detail="read-only · from status" />
      <KeyValueRows rows={rows} className="md:grid-cols-4" />
    </GlassCard>
  );
}
