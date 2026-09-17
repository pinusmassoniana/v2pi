import { useQuery } from "@tanstack/react-query";
import { queries } from "../../api/keys";
import { CardHeader } from "../../components/data/CardHeader";
import { cardFallback, type CardQuery } from "../../components/data/CardState";
import { Chip } from "../../components/data/Chip";
import { GlassCard } from "../../components/ui/GlassCard";
import { cn } from "../../lib/cn";
import { compactBytes } from "../../lib/format";
import { CAP_NOTE, USAGE_NOTE, capFraction, capText, capTone, usageDays, usageRows } from "./usage";

const TONE_BAR: Record<string, string> = { ok: "bg-ok/70", warn: "bg-warn", bad: "bg-bad" };
const TONE_TEXT: Record<string, string> = { ok: "text-t1", warn: "text-warn", bad: "text-bad" };

/**
 * A7: what the gateway has actually moved — today, this week, this billing month and last, with
 * a bar per day. The gateway adds it up in SQL; 90 days is ~129k minute rows, and the chart
 * endpoint would have had to ship every one of them for the browser to do the same.
 */
export function UsageCard({ className }: { className?: string }) {
  const usage = useQuery(queries.trafficUsage());   // read once: a day's total does not need polling
  const data = usage.data;
  const tone = data ? capTone(data.month, data.cap_bytes) : null;
  const fraction = data ? capFraction(data.month, data.cap_bytes) : null;
  const days = data ? usageDays(data) : [];
  const peak = Math.max(1, ...days.map((day) => day.bytes));
  return (
    <GlassCard aria-label="Usage" className={className}>
      <CardHeader
        title="Usage"
        detail={data ? `${data.retention_days} days kept` : "tunnelled traffic"}
        aside={tone ? <Chip tone={tone === "ok" ? "ok" : tone === "warn" ? "warn" : "bad"}>{Math.round((fraction ?? 0) * 100)}% of cap</Chip> : null}
      />
      {cardFallback([usage as CardQuery], "usage did not load", "h-32") ?? (
        <>
          <dl className="grid grid-cols-2 gap-2 md:grid-cols-4">
            {usageRows(data!).map((row) => (
              <div key={row.key} className="min-w-0">
                <dt className="text-[10.5px] font-semibold uppercase tracking-[.07em] text-t3">{row.label}</dt>
                <dd className={cn("mt-0.5 truncate text-[15px] font-bold tracking-tight", row.key === "month" && tone ? TONE_TEXT[tone] : "text-t1")}>
                  {row.value}
                </dd>
              </div>
            ))}
          </dl>

          {fraction !== null ? (
            <div
              role="meter"
              aria-label="Monthly cap"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.round(fraction * 100)}
              className="mt-2.5 h-1.5 overflow-hidden rounded-full bg-glass-2"
            >
              <i className={cn("block h-full rounded-full", TONE_BAR[tone ?? "ok"])} style={{ width: `${Math.round(fraction * 100)}%` }} />
            </div>
          ) : null}
          <p className="mt-1.5 text-xs text-t2">{capText(data!)}</p>

          {days.length ? (
            <ul aria-label="Daily traffic" className="mt-3 flex h-16 items-end gap-0.5">
              {days.map((day) => (
                <li
                  key={day.day}
                  title={`${day.label}: ${compactBytes(day.bytes)}`}
                  className="min-w-0 flex-1 rounded-t-sm bg-g2/40"
                  style={{ height: `${Math.max(2, Math.round((day.bytes / peak) * 100))}%` }}
                />
              ))}
            </ul>
          ) : null}
          {days.length ? (
            <p className="mt-1 flex justify-between text-[10.5px] text-t3">
              <span>{days[0]!.label}</span>
              <span>{days.at(-1)!.label}</span>
            </p>
          ) : null}

          <p className="mt-2 text-[11px] leading-relaxed text-t3">{USAGE_NOTE} {data!.cap_bytes > 0 ? CAP_NOTE : ""}</p>
        </>
      )}
    </GlassCard>
  );
}
