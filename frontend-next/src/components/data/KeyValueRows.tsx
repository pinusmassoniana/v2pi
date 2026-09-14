import type { ReactNode } from "react";
import { cn } from "../../lib/cn";

export interface KeyValueRow {
  key: string;
  value: ReactNode;
  sub?: ReactNode;
}

/** Labelled values in tiles, two to a row. */
export function KeyValueRows({ rows, className }: { rows: readonly KeyValueRow[]; className?: string }) {
  return (
    <dl className={cn("grid grid-cols-2 gap-2", className)}>
      {rows.map((row) => (
        <div key={row.key} className="min-w-0 rounded-xl border border-line bg-glass px-3 py-2">
          <dt className="text-[10.5px] font-semibold tracking-wide text-t3">{row.key}</dt>
          <dd className="mt-0.5 truncate text-[13px] font-semibold text-t1">{row.value}</dd>
          {row.sub ? <dd className="truncate text-[11px] text-t3">{row.sub}</dd> : null}
        </div>
      ))}
    </dl>
  );
}
