import { cn } from "../../lib/cn";
import type { LatencyRowData } from "./types";

export interface LatencyBarsProps {
  rows: readonly LatencyRowData[];
  /** The list's accessible name. */
  label: string;
  /** Full scale of every bar, in ms. */
  scaleMs?: number;
  /** Slower than this reads as a warning. */
  warnMs?: number;
  /** Axis labels under the bars, in ms; none draws no axis. */
  ticks?: readonly number[];
  /** Draw the warning threshold across each track. */
  thresholdLine?: boolean;
}

const WIDE = "col-span-2 text-left text-[11.5px]";

function BarRow({ row, scaleMs, warnMs, thresholdLine }: { row: LatencyRowData; scaleMs: number; warnMs: number; thresholdLine: boolean }) {
  const slow = row.ms !== null && row.ms > warnMs;
  const width = row.ms === null ? 0 : Math.min(100, Math.round((row.ms / scaleMs) * 1000) / 10);
  return (
    <li
      data-state={row.state}
      data-dim={row.dim || undefined}
      data-active={row.active || undefined}
      className={cn(
        "grid grid-cols-[minmax(0,7.5rem)_minmax(0,1fr)_3.25rem_3.75rem] items-center gap-2.5 border-t border-line py-1.5 text-xs first:border-t-0",
        row.active && "my-1 rounded-xl border-t-0 bg-glass-2 px-2 shadow-[inset_0_0_0_1px_var(--line)]",
      )}
    >
      <span className={cn("truncate", row.dim && "opacity-45", row.state === "not-probed" && "text-t2")}>
        {row.flag ? `${row.flag} ` : ""}{row.name}
      </span>
      <span
        aria-hidden
        className={cn(
          "relative h-1.5 rounded-full bg-glass-2",
          row.state === "failed" && "track-failed",
          row.state === "not-probed" && "track-none",
          row.dim && "opacity-45",
        )}
      >
        {width > 0 ? (
          <i className={cn("block h-full rounded-full transition-[width] duration-200", row.active ? "bg-brand" : slow ? "bg-warn" : "bg-ok/70")} style={{ width: `${width}%` }} />
        ) : null}
        {thresholdLine ? <span className="absolute -inset-y-1 w-px bg-warn/50" style={{ left: `${(warnMs / scaleMs) * 100}%` }} /> : null}
      </span>
      {row.state === "live" ? (
        <>
          <span className="text-right font-semibold tabular-nums">{row.ms} ms</span>
          <span className="text-[11px] text-ok">live</span>
        </>
      ) : row.state === "measured" ? (
        <>
          <span className={cn("text-right font-semibold tabular-nums", slow && "text-warn", row.dim && "opacity-45")}>{row.ms} ms</span>
          <span className={cn("whitespace-nowrap text-[11px] text-t3", row.dim && "opacity-45")}>· {row.age}</span>
        </>
      ) : row.state === "failed" && !row.active ? (
        <>
          <span className="text-right text-[11px] font-semibold text-bad">failed</span>
          <span className={cn("whitespace-nowrap text-[11px] text-t3", row.dim && "opacity-45")}>{row.age ? `· ${row.age}` : ""}</span>
        </>
      ) : row.state === "failed" ? (
        <span className={cn(WIDE, "font-semibold text-bad")}>probe failed</span>
      ) : row.state === "stale" ? (
        <span className={cn(WIDE, "font-semibold text-warn")}>health stale</span>
      ) : (
        <span className={cn(WIDE, "italic text-t3")}>not probed</span>
      )}
    </li>
  );
}

/** Latency per node on one shared scale: the active node in the brand gradient, standbys with the age of their probe. */
export function LatencyBars({ rows, label, scaleMs = 250, warnMs = 150, ticks = [0, 125, 250], thresholdLine = false }: LatencyBarsProps) {
  return (
    <div>
      <ul aria-label={label} className="flex flex-col">
        {rows.map((row) => <BarRow key={row.id} row={row} scaleMs={scaleMs} warnMs={warnMs} thresholdLine={thresholdLine} />)}
      </ul>
      {ticks.length > 0 ? (
        <div aria-hidden className="mt-1 grid grid-cols-[minmax(0,7.5rem)_minmax(0,1fr)_7rem] gap-2.5 text-[9.5px] text-t3">
          <span />
          <span className="flex justify-between tabular-nums">{ticks.map((tick) => <span key={tick}>{tick}</span>)}</span>
          <span>ms</span>
        </div>
      ) : null}
    </div>
  );
}
