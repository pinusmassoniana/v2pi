import { cn } from "../../lib/cn";
import type { EventLevel } from "./types";

export interface EventFeedItem {
  key: string;
  /** Already formatted, e.g. local "14:09:40". */
  time: string;
  level: EventLevel;
  kind: string;
  detail: string;
}

const LEVEL: Record<EventLevel, string> = {
  ok: "bg-ok/12 text-ok",
  warn: "bg-warn/12 text-warn",
  bad: "bg-bad/12 text-bad",
  info: "bg-g2/15 text-g2",
};

/** A tail of gateway events: time, level, what happened. */
export function EventFeed({ items, empty = "No recent events.", label = "Recent events" }: { items: readonly EventFeedItem[]; empty?: string; label?: string }) {
  if (items.length === 0) return <p className="py-2 text-sm text-t3">{empty}</p>;
  return (
    <ol aria-label={label} className="flex flex-col">
      {items.map((item) => (
        <li key={item.key} className="grid grid-cols-[4rem_2.75rem_minmax(0,1fr)] items-center gap-2.5 border-t border-line py-1.5 text-xs text-t2 first:border-t-0">
          <time className="font-mono text-t3">{item.time}</time>
          <span data-level={item.level} className={cn("rounded-md py-0.5 text-center text-[9.5px] font-bold uppercase tracking-wider", LEVEL[item.level])}>
            {item.level}
          </span>
          <span className="truncate"><b className="font-semibold text-t1">{item.kind}</b> · {item.detail}</span>
        </li>
      ))}
    </ol>
  );
}
