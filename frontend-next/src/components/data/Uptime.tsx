import { useEffect, useState } from "react";
import { serverNow } from "../../api/clock";
import { cn } from "../../lib/cn";
import { fmtUptime, fmtUptimeCoarse } from "../../lib/format";

export interface UptimeProps {
  /** Epoch seconds the connection started; null when unknown. */
  since: number | null;
  /** Counts only while the tunnel is up; otherwise "—". */
  running: boolean;
  /** "3d 4h" refreshed every 30 s instead of "3d 04:12:09" every second. */
  coarse?: boolean;
  className?: string;
}

/** Owns its own clock, so a ticking uptime re-renders this span — not the page around it. */
export function Uptime({ since, running, coarse = false, className }: UptimeProps) {
  const [, setTick] = useState(0);
  const counting = running && since !== null;

  useEffect(() => {
    if (!counting) return;
    const timer = setInterval(() => setTick((tick) => tick + 1), coarse ? 30_000 : 1_000);
    return () => clearInterval(timer);
  }, [counting, coarse]);

  if (!running || since === null) return <span className={className}>—</span>;
  const seconds = Math.max(0, Math.floor(serverNow() / 1000 - since));
  return (
    <span aria-live="off" className={cn("tabular-nums", !coarse && "font-mono", className)}>
      {coarse ? fmtUptimeCoarse(seconds) : fmtUptime(seconds)}
    </span>
  );
}
