import { Link } from "@tanstack/react-router";
import { ChevronDown, ChevronUp } from "lucide-react";
import { memo } from "react";
import type { Node, NodeHealth } from "../../api/client";
import { Ago } from "../../components/data/Ago";
import { Sparkline } from "../../components/data/Sparkline";
import { Uptime } from "../../components/data/Uptime";
import { Button } from "../../components/ui/Button";
import { cn } from "../../lib/cn";
import type { SortDir, SortKey } from "./list";
import { NodeRowActions, type NodeMenuCallbacks } from "./NodeRowActions";
import { ActiveRealPill, Egress, FailBadge, ProbePill, StaleBadge } from "./probe";
import { detailLinkSearch } from "./search";

export interface ReorderControls {
  busy: boolean;
  /** Swap a node with its neighbour in the group's current order. */
  onMove: (nodeId: number, delta: -1 | 1) => void;
}

/** N18: the selection over the shown rows. */
export interface SelectionControls {
  selected: ReadonlySet<number>;
  /** The select-all checkbox: none, some (indeterminate) or all of the shown rows. */
  state: "none" | "some" | "all";
  onToggle: (id: number) => void;
  onToggleAll: () => void;
}

/** What the desktop table and the phone cards share. */
export interface NodeListProps {
  /** The rows to render, already searched, sorted and capped. */
  rows: readonly Node[];
  health: ReadonlyMap<number, NodeHealth>;
  activeId: number | null;
  /** When the active node was connected (epoch s). */
  activeSince: number | null;
  dense: boolean;
  menu: NodeMenuCallbacks;
  /** Up / down arrows (N11); null when this list cannot be reordered. */
  reorder: ReorderControls | null;
  /** Checkboxes (N18): always on the desktop table; on phone cards only in select mode. Null for none. */
  selection: SelectionControls | null;
}

export interface NodeTableProps extends NodeListProps {
  sort: SortKey;
  dir: SortDir;
  onSort: (key: SortKey) => void;
}

/** Hidden columns fold into the name block below these widths (spec §4). */
const WIDE = "hidden min-[1281px]:table-cell";
const MEDIUM = "hidden min-[1025px]:table-cell";
const NARROW = "hidden min-[901px]:table-cell";

interface RowProps {
  node: Node;
  /** First and last row of a reorderable list; both false otherwise, so a search that shifts rows leaves them be. */
  first: boolean;
  last: boolean;
  health: NodeHealth | undefined;
  active: boolean;
  activeSince: number | null;
  dense: boolean;
  menu: NodeMenuCallbacks;
  reorder: ReorderControls | null;
  /** Null when the list shows no checkboxes. */
  selected: boolean | null;
  onToggle: (id: number) => void;
}

const noToggle = () => {};

/** One row; memoised, so a poll that changes one node's health re-renders that row only. */
const NodeRow = memo(function NodeRow({ node, first, last, health, active, activeSince, dense, menu, reorder, selected, onToggle }: RowProps) {
  const cell = cn("px-2.5 align-middle", dense ? "py-1" : "py-2.5");
  const failCount = active ? (health?.fail_count ?? 0) : 0;
  return (
    <tr
      data-node-id={node.id}
      data-node-name={node.name}
      data-active={active || undefined}
      data-stale={node.stale || undefined}
      className={cn("border-t border-line transition-opacity duration-150", node.stale && "opacity-55", active && "bg-glass-2 shadow-[inset_3px_0_0_var(--g2)]")}
    >
      {selected !== null ? (
        <td className={cn(cell, "w-px")}>
          <input type="checkbox" aria-label={`Select ${node.name}`} checked={selected} onChange={() => onToggle(node.id)} className="size-4 accent-g2" />
        </td>
      ) : null}
      <td className={cn(cell, "min-w-0")}>
        <div className="flex items-start gap-2">
          {reorder ? (
            <span className="flex shrink-0 flex-col">
              <Button size="icon" variant="ghost" className="size-6" aria-label={`Move ${node.name} up`} disabled={first || reorder.busy} onClick={() => reorder.onMove(node.id, -1)}>
                <ChevronUp size={14} aria-hidden />
              </Button>
              <Button size="icon" variant="ghost" className="size-6" aria-label={`Move ${node.name} down`} disabled={last || reorder.busy} onClick={() => reorder.onMove(node.id, 1)}>
                <ChevronDown size={14} aria-hidden />
              </Button>
            </span>
          ) : null}
          <div className="min-w-0">
            <div className="flex min-w-0 flex-wrap items-center gap-1.5">
              <Link to="/nodes/$nodeId" params={{ nodeId: String(node.id) }} search={detailLinkSearch} className="truncate text-[13px] font-semibold text-t1 hover:underline">
                {node.name}
              </Link>
              {node.stale ? <StaleBadge /> : null}
              {failCount > 0 ? <FailBadge count={failCount} /> : null}
            </div>
            {active ? (
              <p className="text-[11px] font-semibold text-ok">
                connected{activeSince !== null ? <> · <Uptime since={activeSince} running coarse /></> : null}
              </p>
            ) : null}
            <p className="truncate text-[11px] text-t3">
              <span className="font-mono">#{node.id}</span>
              <span className="min-[1281px]:hidden"> · {node.port} · {node.transport} · {node.security}</span>
              {node.note ? <span className="text-t2"> · {node.note}</span> : null}
            </p>
          </div>
        </div>
      </td>
      <td className={cn(cell, "max-w-56 truncate font-mono text-[11.5px] text-t2")} title={node.address}>{node.address}</td>
      <td className={cn(cell, WIDE, "font-mono text-[11.5px] tabular-nums text-t2")}>{node.port}</td>
      <td className={cn(cell, WIDE, "whitespace-nowrap text-t2")}>{node.transport} · {node.security}</td>
      <td className={cell}><ProbePill ok={health?.last_tcp_ok} ms={health?.last_tcp_ms} /></td>
      <td className={cell}><ProbePill ok={health?.last_http_ok} ms={health?.last_http_ms} /></td>
      <td className={cell}>{active ? <ActiveRealPill nodeId={node.id} health={health} /> : <ProbePill ok={health?.last_real_ok} ms={health?.last_real_ms} />}</td>
      <td className={cn(cell, NARROW, "max-w-44")}><Egress health={health} /></td>
      <td className={cn(cell, MEDIUM, "w-20")}>
        {health && health.lat_history.length > 1 ? <Sparkline values={health.lat_history} width={64} height={18} className="h-[18px] w-16" /> : <span className="text-t3">—</span>}
      </td>
      <td className={cn(cell, MEDIUM, "whitespace-nowrap text-t3")} title={health?.checked_at ?? undefined}><Ago at={health?.checked_at} /></td>
      <td className={cn(cell, "w-px")}><NodeRowActions node={node} active={active} menu={menu} /></td>
    </tr>
  );
});

const SORTABLE: readonly { key: SortKey; label: string; className?: string }[] = [
  { key: "name", label: "Name" }, { key: "address", label: "Address" },
];

function SortHeader({ label, sortKey, sort, dir, onSort, className }: { label: string; sortKey: SortKey; sort: SortKey; dir: SortDir; onSort: (key: SortKey) => void; className?: string }) {
  const on = sort === sortKey;
  return (
    <th scope="col" aria-sort={on ? (dir === "asc" ? "ascending" : "descending") : undefined} className={cn("px-2.5 py-2 font-semibold", className)}>
      <button type="button" onClick={() => onSort(sortKey)} className={cn("inline-flex items-center gap-1 uppercase tracking-[.07em] hover:text-t1", on && "text-t1")}>
        {label}
        {on ? <span aria-hidden>{dir === "asc" ? "▲" : "▼"}</span> : null}
      </button>
    </th>
  );
}

/** N5 on a desktop: a real table, sortable by its headers, whose columns collapse by breakpoint. */
export function NodeTable({ rows, health, activeId, activeSince, dense, menu, reorder, selection, sort, dir, onSort }: NodeTableProps) {
  const plain = "px-2.5 py-2 font-semibold uppercase tracking-[.07em]";
  return (
    <div className="glass overflow-x-auto">
      <table aria-label="Nodes" data-dense={dense || undefined} className="w-full text-left text-xs">
        <thead className="text-[9.5px] text-t3">
          <tr>
            {selection ? (
              <th scope="col" className="w-px px-2.5 py-2">
                <input
                  type="checkbox"
                  aria-label="Select all shown"
                  checked={selection.state === "all"}
                  ref={(input) => { if (input) input.indeterminate = selection.state === "some"; }}
                  onChange={selection.onToggleAll}
                  className="size-4 accent-g2"
                />
              </th>
            ) : null}
            {SORTABLE.map((column) => <SortHeader key={column.key} label={column.label} sortKey={column.key} sort={sort} dir={dir} onSort={onSort} />)}
            <th scope="col" className={cn(plain, WIDE)}>Port</th>
            <th scope="col" className={cn(plain, WIDE)}>Transport</th>
            <SortHeader label="TCP" sortKey="tcp" sort={sort} dir={dir} onSort={onSort} />
            <SortHeader label="HTTP" sortKey="http" sort={sort} dir={dir} onSort={onSort} />
            <th scope="col" className={plain}>Real</th>
            <th scope="col" className={cn(plain, NARROW)}>Egress</th>
            <th scope="col" className={cn(plain, MEDIUM)}>Trend</th>
            <th scope="col" className={cn(plain, MEDIUM)}>Checked</th>
            <th scope="col" className="px-2.5 py-2"><span className="sr-only">Actions</span></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((node, index) => (
            <NodeRow
              key={node.id}
              node={node}
              first={reorder !== null && index === 0}
              last={reorder !== null && index === rows.length - 1}
              health={health.get(node.id)}
              active={node.id === activeId}
              activeSince={node.id === activeId ? activeSince : null}
              dense={dense}
              menu={menu}
              reorder={reorder}
              selected={selection ? selection.selected.has(node.id) : null}
              onToggle={selection?.onToggle ?? noToggle}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}
