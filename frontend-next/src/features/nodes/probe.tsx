import { useCallback, useEffect, useState } from "react";
import type { NodeHealth } from "../../api/client";
import { serverNow } from "../../api/clock";
import { useTrafficSelect, type TrafficSnapshot } from "../../api/traffic";
import type { Tone } from "../../components/data/types";
import { Pill } from "../../components/ui/Pill";
import { cn } from "../../lib/cn";
import { flagEmoji } from "../../lib/flag";
import { probeFor } from "../../lib/nodeHealth";
import { checkedAgo, liveReading, pillTone, type PillState } from "./list";

const TONE: Record<PillState, Tone> = { ok: "ok", slow: "warn", failed: "bad", unknown: "neutral" };

export interface ProbePillProps {
  ok: boolean | null | undefined;
  ms: number | null | undefined;
  /** "TCP", "HTTP" or "real": shown inside the pill on phone cards, where there is no column header. */
  label?: string;
  /** The reading comes from the live traffic frame. */
  live?: boolean;
  className?: string;
}

/** N5: a probe result as text in its state colour — "42 ms", "164 ms" (slow), "failed", or "—". */
export function ProbePill({ ok, ms, label, live = false, className }: ProbePillProps) {
  const state = pillTone(ok, ms);
  const text = state === "failed" ? "failed" : state === "unknown" ? "—" : ms === null || ms === undefined ? "ok" : `${ms} ms`;
  return (
    <Pill
      tone={TONE[state]}
      data-state={state}
      data-live={live || undefined}
      title={live ? "live probe" : undefined}
      className={cn("gap-1 px-2 py-0.5 text-[11px] tabular-nums", className)}
    >
      {label ? <span className="font-normal text-t3">{label}</span> : null}
      {text}
    </Pill>
  );
}

/**
 * The active node's real-check pill: the live frame's reading while it is fresh, else the last sweep's. Only this
 * component reads the traffic store, so a frame every second re-renders this pill and nothing around it.
 */
export function ActiveRealPill({ nodeId, health, label }: { nodeId: number; health: NodeHealth | undefined; label?: string }) {
  const select = useCallback(
    (snapshot: TrafficSnapshot) => (snapshot.disabled || !snapshot.fresh ? null : liveReading(probeFor(snapshot.live, nodeId))),
    [nodeId],
  );
  const live = useTrafficSelect(select);
  if (live === null) return <ProbePill ok={health?.last_real_ok} ms={health?.last_real_ms} label={label} />;
  return <ProbePill ok={live !== "failed"} ms={live === "failed" ? null : live} label={label} live />;
}

/** How often {@link CheckedAgo} re-renders itself to keep its age from freezing. */
const CHECKED_AGO_TICK_MS = 15_000;

/**
 * N5 "checked": owns its own coarse timer, so the age keeps advancing even on a row that is otherwise memoised
 * and would not re-render again until its health changes (the background sweep runs every 30 min by default).
 */
export function CheckedAgo({ at }: { at: string | null | undefined }) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setTick((tick) => tick + 1), CHECKED_AGO_TICK_MS);
    return () => clearInterval(timer);
  }, []);
  return <>{checkedAgo(at, serverNow()) ?? "—"}</>;
}

/** N5 egress: the probed exit address with its country's flag, IPv6 underneath. */
export function Egress({ health, className }: { health: NodeHealth | undefined; className?: string }) {
  const flag4 = flagEmoji(health?.egress_cc);
  const flag6 = flagEmoji(health?.egress_cc6);
  return (
    <span className={cn("block min-w-0 font-mono text-[11px] text-t2", className)}>
      <span className="block truncate" title={health?.egress_cc ?? undefined}>{flag4 ? `${flag4} ` : ""}{health?.egress_ip ?? "—"}</span>
      {health?.egress_ip6 ? <span className="block truncate text-t3" title={health.egress_cc6 ?? undefined}>{flag6 ? `${flag6} ` : ""}{health.egress_ip6}</span> : null}
    </span>
  );
}

/** "stale": the node vanished from its subscription and was kept. */
export function StaleBadge() {
  return (
    <span
      title="Vanished from its subscription; kept because it was active or for history"
      className="shrink-0 rounded-full border border-line bg-glass-2 px-1.5 text-[10px] font-bold uppercase tracking-wide text-t3"
    >
      stale
    </span>
  );
}

/** "fail N": the active node's consecutive real-request failures. */
export function FailBadge({ count }: { count: number }) {
  return (
    <span title="Consecutive real-request failures (auto-failover counter)" className="shrink-0 rounded-full bg-bad/12 px-1.5 text-[10px] font-bold text-bad">
      fail {count}
    </span>
  );
}
