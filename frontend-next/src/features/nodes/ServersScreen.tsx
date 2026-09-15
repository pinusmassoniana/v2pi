import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useRouterState, useSearch } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import type { Node } from "../../api/client";
import { SLOW_POLL_MS } from "../../api/cadence";
import { useApiWrite } from "../../api/invalidation";
import { keys, queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { cardFallback, staleNotice } from "../../components/data/CardState";
import { EmptyState, Skeleton } from "../../components/ui/States";
import { notifyError } from "../../components/ui/Toaster";
import { DESKTOP_QUERY, useMediaQuery } from "../../lib/media";
import { FailoverNote } from "./FailoverNote";
import {
  SERVERS, applyOrder, canReorder, groupChips, healthById, moveWithin, resolveGroup, shownNodes, visibleRows, type GroupKey, type SortKey,
} from "./list";
import { NodeCards } from "./NodeCards";
import { NO_MENU_CALLBACKS } from "./NodeRowActions";
import { NodeTable } from "./NodeTable";
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

/** N11: reorder the Servers group. The cache takes the new order at once; a failed write puts the server's back. */
function useReorder(shown: readonly Node[]) {
  const queryClient = useQueryClient();
  const reorderWrite = useApiWrite("reorderNodes");
  const { mutate, isPending } = useMutation({
    mutationFn: (ids: number[]) => reorderWrite(ids),
    onMutate: (ids) => queryClient.setQueryData<Node[]>(keys.nodes, (old) => (old ? applyOrder(old, ids) : old)),
    onError: (error) => {
      notifyError(error, "reorder failed");
      void queryClient.invalidateQueries({ queryKey: keys.nodes });
    },
  });
  const onMove = useCallback((index: number, delta: -1 | 1) => {
    if (isPending) return;
    const ids = moveWithin(shown.map((node) => node.id), index, delta);
    if (ids) mutate(ids);
  }, [shown, isPending, mutate]);
  return useMemo(() => ({ busy: isPending, onMove }), [isPending, onMove]);
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
    const common = { rows, health: healthMap, activeId, activeSince, dense, menu: NO_MENU_CALLBACKS, detailSearch, reorder };
    body = desktop ? <NodeTable {...common} sort={sort} dir={dir} onSort={onSort} /> : <NodeCards {...common} />;
  }

  return (
    <div className="flex flex-col gap-3">
      <ServersToolbar chips={chips} group={group} list={list} onList={onList} entry={entry} dense={dense} onDense={onDense} />
      <FailoverNote status={status.data} className="px-1" />
      {status.isError ? <p className="px-1 text-xs font-semibold text-bad">{OFFLINE_HINT} — connecting is unavailable until it answers.</p> : null}
      {ready ? staleNotice([nodes, health, subs], "Servers did not refresh") : null}
      {body}
      {ready ? <RowCapFooter shown={rows.length} total={shown.length} onShowAll={() => setShowAllGroup(group)} /> : null}
      {children}
    </div>
  );
}

/** The #/nodes route. */
export function Servers() {
  return <ServersView search={useSearch({ from: "/nodes" })} />;
}
