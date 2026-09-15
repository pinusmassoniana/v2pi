import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useRouterState, useSearch } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import type { Node } from "../../api/client";
import { SLOW_POLL_MS } from "../../api/cadence";
import { useApiWrite } from "../../api/invalidation";
import { keys, queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { cardFallback, staleNotice } from "../../components/data/CardState";
import { Button } from "../../components/ui/Button";
import { EmptyState, Skeleton } from "../../components/ui/States";
import { notifyError } from "../../components/ui/Toaster";
import { DESKTOP_QUERY, useMediaQuery } from "../../lib/media";
import { BulkBar } from "./BulkBar";
import { FailoverNote } from "./FailoverNote";
import { GroupActions } from "./GroupActions";
import {
  SERVERS, applyOrder, canReorder, groupChips, healthById, moveWithin, pruneSelection, resolveGroup, selectionState, shownNodes, visibleRows,
  type GroupKey, type SortKey,
} from "./list";
import { NodeCards } from "./NodeCards";
import { NO_MENU_CALLBACKS } from "./NodeRowActions";
import { NodeTable, type SelectionControls } from "./NodeTable";
import { RowCapFooter } from "./RowCapFooter";
import { listState, readDense, toSearch, writeDense, type ListState, type NodesSearch } from "./search";
import { ServersToolbar } from "./ServersToolbar";
import { OFFLINE_HINT } from "./useNodeActions";

export interface ServersViewProps {
  search: NodesSearch;
  /** A node's detail shows the list of that node's group, whatever the URL's group says. */
  groupOverride?: GroupKey;
  /** Rendered after the list: the node detail sheet. */
  children?: ReactNode;
}

function LoadingRows() {
  return (
    <div role="status" aria-busy className="glass flex flex-col gap-2 p-4">
      <p className="text-sm text-t2">Loading servers…</p>
      {[0, 1, 2, 3].map((row) => <Skeleton key={row} className="h-9" />)}
    </div>
  );
}

/**
 * N11: reorder the Servers group. The cache takes the new order at once; a failed write puts it back exactly
 * as it was. Cancelling the nodes query first stops an in-flight refetch (the previous move's, or the 30 s
 * poll's) from landing after this optimistic update and reverting it out from under the next move.
 */
function useReorder(shown: readonly Node[]) {
  const queryClient = useQueryClient();
  const reorderWrite = useApiWrite("reorderNodes");
  const { mutate, isPending } = useMutation({
    mutationFn: (ids: number[]) => reorderWrite(ids),
    onMutate: async (ids: number[]) => {
      await queryClient.cancelQueries({ queryKey: keys.nodes });
      const previous = queryClient.getQueryData<Node[]>(keys.nodes);
      queryClient.setQueryData<Node[]>(keys.nodes, (old) => (old ? applyOrder(old, ids) : old));
      return { previous };
    },
    onError: (error, _ids, context) => {
      notifyError(error, "reorder failed");
      if (context?.previous) queryClient.setQueryData(keys.nodes, context.previous);
    },
  });
  const onMove = useCallback((index: number, delta: -1 | 1) => {
    if (isPending) return;
    const ids = moveWithin(shown.map((node) => node.id), index, delta);
    if (ids) mutate(ids);
  }, [shown, isPending, mutate]);
  return useMemo(() => ({ busy: isPending, onMove }), [isPending, onMove]);
}

const NONE: ReadonlySet<number> = new Set();

/**
 * N18: the selection belongs to its group — switching group starts empty, even coming back to a group visited
 * earlier in the same session — and only rows still shown count as selected, while a node hidden by the search
 * keeps its tick for when it shows again.
 */
function useSelection(group: GroupKey, shown: readonly Node[]) {
  const [ids, setIds] = useState<ReadonlySet<number>>(NONE);
  // Adjusting state while rendering (plain state, not a ref — react-hooks 7 forbids reading/writing ref.current
  // during render) — a group change drops the old ticks in the same render, not an effect a frame later.
  const [seenGroup, setSeenGroup] = useState(group);
  if (seenGroup !== group) {
    setSeenGroup(group);
    if (ids.size > 0) setIds(NONE);
  }
  const selected = useMemo(() => pruneSelection(ids, shown), [ids, shown]);
  const state = selectionState(selected, shown);
  const onToggle = useCallback((id: number) => setIds((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  }), []);
  const onToggleAll = useCallback(
    () => setIds(state === "all" ? NONE : new Set(shown.map((node) => node.id))),
    [shown, state],
  );
  const clear = useCallback(() => setIds(NONE), []);
  const controls: SelectionControls = useMemo(() => ({ selected, state, onToggle, onToggleAll }), [selected, state, onToggle, onToggleAll]);
  const nodes = useMemo(() => shown.filter((node) => selected.has(node.id)), [shown, selected]);
  return { controls, nodes, clear };
}

/**
 * Nodes › Servers: the polling owner of nodes, node health and subscriptions while it is mounted (the detail route
 * mounts it too, on a desktop). The search, sort and group live in the URL; density in localStorage.
 */
export function ServersView({ search, groupOverride, children }: ServersViewProps) {
  const navigate = useNavigate();
  const desktop = useMediaQuery(DESKTOP_QUERY);
  const status = useQuery(queries.status());
  const nodes = usePolledQuery(queries.nodes(), SLOW_POLL_MS);
  const health = usePolledQuery(queries.nodeHealth(), SLOW_POLL_MS);
  const subs = usePolledQuery(queries.subs(), SLOW_POLL_MS);
  const entry = useRouterState({ select: (state) => state.location.state.__TSR_index });
  const [dense, setDense] = useState(readDense);
  const [showAllGroup, setShowAllGroup] = useState<GroupKey | null>(null);
  const [selecting, setSelecting] = useState(false);   // phone select mode

  const list: ListState = { ...listState(search), group: groupOverride ?? search.group };
  const resolved = groupOverride !== undefined ? { group: groupOverride, replace: false } : resolveGroup(search.group, subs.data);
  const group = resolved.group;
  const { q, sort, dir } = list;
  const healthMap = useMemo(() => healthById(health.data), [health.data]);
  const chips = useMemo(() => groupChips(nodes.data ?? [], subs.data ?? []), [nodes.data, subs.data]);
  const shown = useMemo(() => shownNodes(nodes.data ?? [], healthMap, group, q, sort, dir), [nodes.data, healthMap, group, q, sort, dir]);
  const rows = visibleRows(shown, showAllGroup === group);
  const detailSearch = useMemo(() => toSearch({ q, sort, dir }), [q, sort, dir]);
  const reorderControls = useReorder(shown);
  const reorder = canReorder(group, sort, q) ? reorderControls : null;
  const selection = useSelection(group, shown);

  // N1: a group that does not exist (any more) falls back to the default, without a history entry of its own.
  useEffect(() => {
    if (resolved.replace) void navigate({ to: "/nodes", search: (prev) => ({ ...prev, group: undefined }), replace: true });
  }, [resolved.replace, navigate]);

  const listGroup = list.group;
  const onList = useCallback(
    (patch: Partial<ListState>) => void navigate({ to: "/nodes", search: toSearch({ group: listGroup, q, sort, dir, ...patch }), replace: true }),
    [navigate, listGroup, q, sort, dir],
  );
  const onSort = useCallback(
    (key: SortKey) => onList(key === sort ? { dir: dir === "asc" ? "desc" : "asc" } : { sort: key, dir: "asc" }),
    [onList, sort, dir],
  );
  const onDense = useCallback((next: boolean) => {
    setDense(next);
    writeDense(next);
  }, []);

  const ready = nodes.data !== undefined && subs.data !== undefined;
  const failed = (nodes.isError && nodes.data === undefined) || (subs.isError && subs.data === undefined);
  const activeId = status.data?.active_node_id ?? null;
  const activeSince = status.data?.active_since ?? null;
  let body: ReactNode;
  if (failed) body = cardFallback([nodes, subs], "Servers did not load");
  else if (!ready) body = <LoadingRows />;
  else if (shown.length === 0) body = <EmptyState title={group === SERVERS ? "No servers here — add one with Add server." : "No servers here"} />;
  else {
    const common = {
      rows, health: healthMap, activeId, activeSince, dense, menu: NO_MENU_CALLBACKS, detailSearch, reorder,
      selection: desktop || selecting ? selection.controls : null,
    };
    body = desktop ? <NodeTable {...common} sort={sort} dir={dir} onSort={onSort} /> : <NodeCards {...common} />;
  }

  return (
    <div className="flex flex-col gap-3">
      {!desktop && ready ? (
        <div className="flex items-center justify-end gap-2">
          <Button
            size="sm"
            aria-pressed={selecting}
            onClick={() => {
              if (selecting) selection.clear();
              setSelecting(!selecting);
            }}
          >
            {selecting ? "Done" : "Select"}
          </Button>
        </div>
      ) : null}
      <ServersToolbar
        chips={chips}
        group={group}
        list={list}
        onList={onList}
        entry={entry}
        dense={dense}
        onDense={onDense}
        actions={<GroupActions group={group} shown={shown} nodes={nodes.data} offline={status.isError} />}
      />
      <FailoverNote status={status.data} className="px-1" />
      {status.isError ? <p className="px-1 text-xs font-semibold text-bad">{OFFLINE_HINT} — connecting is unavailable until it answers.</p> : null}
      {ready ? staleNotice([nodes, health, subs], "Servers did not refresh") : null}
      {body}
      {ready ? <RowCapFooter shown={rows.length} total={shown.length} onShowAll={() => setShowAllGroup(group)} /> : null}
      {selection.nodes.length > 0 ? <BulkBar group={group} selected={selection.nodes} onClear={selection.clear} /> : null}
      {children}
    </div>
  );
}

/** The #/nodes route. */
export function Servers() {
  return <ServersView search={useSearch({ from: "/nodes" })} />;
}
