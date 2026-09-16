import { ArrowDownWideNarrow, ArrowUpNarrowWide, Rows3, Rows4 } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Button } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import { Select } from "../../components/ui/Select";
import { cn } from "../../lib/cn";
import { GroupChips } from "./GroupChips";
import type { GroupChip, GroupKey, SortKey } from "./list";
import type { ListState } from "./search";

const SORT_LABELS: Record<SortKey, string> = { pos: "Position", name: "Name", address: "Address", tcp: "TCP", http: "HTTP" };

/**
 * Typing writes the URL (replacing the entry) while the field keeps what was typed; the parent remounts it (a new
 * `key`) when the URL moves to another history entry, so Back and Forward show their own search.
 */
function SearchField({ q, onChange }: { q: string; onChange: (q: string) => void }) {
  const [text, setText] = useState(q);
  return (
    <Input
      type="search"
      aria-label="Search nodes"
      placeholder="Search name, address, note"
      value={text}
      onChange={(event) => {
        setText(event.target.value);
        onChange(event.target.value);
      }}
      className="min-w-0 flex-1 md:max-w-72"
    />
  );
}

export interface ServersToolbarProps {
  chips: readonly GroupChip[];
  group: GroupKey;
  list: ListState;
  onList: (patch: Partial<ListState>) => void;
  /** Changes when the URL moves to another history entry: resets the search field to the URL's search. */
  entry: number;
  dense: boolean;
  onDense: (dense: boolean) => void;
  /** Group actions (ping, test all, connect best). */
  actions?: ReactNode;
  /** Add server / Import, on the Servers group only. */
  manage?: ReactNode;
  className?: string;
}

/** N1–N4 and the group actions: chips, search, sort with direction, density. */
export function ServersToolbar({ chips, group, list, onList, entry, dense, onDense, actions, manage, className }: ServersToolbarProps) {
  const descending = list.dir === "desc";
  return (
    <section aria-label="Servers toolbar" className={cn("glass flex flex-col gap-2.5 p-3", className)}>
      <div className="flex flex-col gap-2.5 md:flex-row md:flex-wrap md:items-center">
        <GroupChips chips={chips} group={group} list={list} />
        {actions ? <div className="grid grid-cols-4 gap-1.5 md:ml-auto md:flex md:flex-wrap">{actions}</div> : null}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <SearchField key={entry} q={list.q} onChange={(q) => onList({ q })} />
        <Select aria-label="Sort by" value={list.sort} onChange={(event) => onList({ sort: event.target.value as SortKey })}>
          {(Object.keys(SORT_LABELS) as SortKey[]).map((key) => <option key={key} value={key}>{SORT_LABELS[key]}</option>)}
        </Select>
        <Button
          size="icon"
          aria-label={descending ? "Sort descending" : "Sort ascending"}
          title={descending ? "Descending" : "Ascending"}
          onClick={() => onList({ dir: descending ? "asc" : "desc" })}
          className="size-10"
        >
          {descending ? <ArrowDownWideNarrow size={16} aria-hidden /> : <ArrowUpNarrowWide size={16} aria-hidden />}
        </Button>
        <Button
          size="icon"
          aria-label="Compact rows"
          aria-pressed={dense}
          title={dense ? "Compact rows" : "Comfortable rows"}
          onClick={() => onDense(!dense)}
          className="size-10"
        >
          {dense ? <Rows4 size={16} aria-hidden /> : <Rows3 size={16} aria-hidden />}
        </Button>
        {manage ? <div className="ml-auto flex items-center gap-2">{manage}</div> : null}
      </div>
    </section>
  );
}
