import type { ReactNode } from "react";
import { cn } from "../../lib/cn";

/** A card's uppercase title, an optional detail beside it, and actions or links on the right. */
export function CardHeader({ title, detail, aside, className }: { title: string; detail?: ReactNode; aside?: ReactNode; className?: string }) {
  return (
    <div className={cn("mb-2 flex min-h-6 items-center justify-between gap-2.5", className)}>
      <div className="flex min-w-0 items-baseline gap-2">
        <h2 className="truncate text-[10.5px] font-semibold uppercase tracking-[.07em] text-t3">{title}</h2>
        {detail ? <span className="truncate text-[11px] text-t3">{detail}</span> : null}
      </div>
      {aside ? <div className="flex shrink-0 items-center gap-2">{aside}</div> : null}
    </div>
  );
}
