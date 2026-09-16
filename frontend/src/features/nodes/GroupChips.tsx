import { Link } from "@tanstack/react-router";
import { cn } from "../../lib/cn";
import type { GroupChip, GroupKey } from "./list";
import { toSearch, type ListState } from "./search";

/** N1: one chip per subscription, then Servers, each with its node count; picking one keeps the search and sort. */
export function GroupChips({ chips, group, list }: { chips: readonly GroupChip[]; group: GroupKey; list: ListState }) {
  return (
    <nav aria-label="Node groups" className="-mx-1 flex min-w-0 gap-1.5 overflow-x-auto px-1 py-0.5 md:flex-wrap md:overflow-visible">
      {chips.map((chip) => {
        const on = chip.key === group;
        return (
          <Link
            key={String(chip.key)}
            to="/nodes"
            search={toSearch({ ...list, group: chip.key })}
            aria-current={on ? "page" : undefined}
            className={cn(
              "inline-flex min-h-9 shrink-0 items-center gap-1.5 rounded-full border border-line px-3 text-xs font-semibold text-t2 transition-colors duration-150 hover:text-t1",
              on && "bg-glass-2 text-t1 shadow-[inset_0_0_0_1px_var(--line)]",
            )}
          >
            {chip.label} <span className="font-normal tabular-nums text-t3">{chip.count}</span>
          </Link>
        );
      })}
    </nav>
  );
}
