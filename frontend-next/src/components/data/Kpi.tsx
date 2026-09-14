import type { ReactNode } from "react";
import { cn } from "../../lib/cn";
import { Sparkline } from "./Sparkline";
import type { Tone } from "./types";

const VALUE: Record<Tone, string> = { ok: "text-ok", warn: "text-warn", bad: "text-bad", neutral: "text-t1" };

export interface KpiProps {
  /** Also the card's accessible name. */
  label: string;
  value: ReactNode;
  unit?: string;
  sub?: ReactNode;
  spark?: readonly number[];
  sparkSeries?: "down" | "up";
  /** Top-right of the card, e.g. the live chip. */
  aside?: ReactNode;
  tone?: Tone;
  /** The value is stale: shown, but dimmed. */
  dim?: boolean;
  /** Heading level of the label: 2 for a card on the page, 3 inside a section that already has an h2. */
  level?: 2 | 3;
  className?: string;
}

export function Kpi({ label, value, unit, sub, spark, sparkSeries = "down", aside, tone = "neutral", dim = false, level = 2, className }: KpiProps) {
  const Heading = level === 2 ? "h2" : "h3";
  return (
    <section
      aria-label={label}
      data-dim={dim || undefined}
      className={cn("glass flex min-w-0 flex-col gap-1 p-3.5 transition-opacity duration-200", dim && "opacity-60", className)}
    >
      <div className="flex items-center justify-between gap-2">
        <Heading className="truncate text-[10.5px] font-semibold uppercase tracking-[.07em] text-t3">{label}</Heading>
        {aside}
      </div>
      <p className={cn("truncate text-[26px] font-bold leading-tight tracking-tight tabular-nums", VALUE[tone])}>
        {value}
        {unit ? <span className="ml-1 text-[11px] font-semibold tracking-normal text-t3">{unit}</span> : null}
      </p>
      {sub ? <div className="truncate text-[11.5px] text-t3">{sub}</div> : null}
      {spark && spark.length > 1 ? <Sparkline values={spark} series={sparkSeries} className="mt-auto" /> : null}
    </section>
  );
}
