import type { ReactNode } from "react";
import { cn } from "../../lib/cn";

/**
 * A card's uppercase title, an optional detail beside it, and actions or links on the right. `level` 3 for a card
 * inside a section (a sheet or a page part) that already has its own h2.
 */
export function CardHeader({ title, detail, aside, level = 2, className }: {
  title: string; detail?: ReactNode; aside?: ReactNode; level?: 2 | 3; className?: string;
}) {
  const Heading = level === 2 ? "h2" : "h3";
  return (
    <div className={cn("mb-2 flex min-h-6 items-center justify-between gap-2.5", className)}>
      <div className="flex min-w-0 items-baseline gap-2">
        <Heading className="truncate text-[10.5px] font-semibold uppercase tracking-[.07em] text-t3">{title}</Heading>
        {detail ? <span className="truncate text-[11px] text-t3">{detail}</span> : null}
      </div>
      {aside ? <div className="flex shrink-0 items-center gap-2">{aside}</div> : null}
    </div>
  );
}
