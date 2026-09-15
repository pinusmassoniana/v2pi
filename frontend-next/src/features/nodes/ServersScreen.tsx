import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Outlet, useNavigate, useParams, useRouterState, useSearch } from "@tanstack/react-router";
import { Ellipsis, Plus, Upload } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import type { Node, NodeHealth } from "../../api/client";
import { SLOW_POLL_MS } from "../../api/cadence";
import { useApiWrite } from "../../api/invalidation";
import { keys, queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { cardFallback, staleNotice } from "../../components/data/CardState";
import { Button } from "../../components/ui/Button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "../../components/ui/DropdownMenu";
import { EmptyState, Skeleton } from "../../components/ui/States";
import { notifyError } from "../../components/ui/Toaster";
import { DESKTOP_QUERY, useMediaQuery } from "../../lib/media";
import { BulkBar } from "./BulkBar";
import { FailoverNote } from "./FailoverNote";
import { GroupActions } from "./GroupActions";
import {
  SERVERS, applyOrder, canReorder, groupChips, groupOf, healthById, inScope, moveWithin, parseGroup, pruneSelection, resolveGroup, selectionState, shownNodes,
  visibleRows, type GroupKey, type SortKey,
} from "./list";
import { NodeCards } from "./NodeCards";
import { NodeDialogs, useNodeDialogs } from "./NodeDialogs";
import { NodeTable, type SelectionControls } from "./NodeTable";
import { RowCapFooter } from "./RowCapFooter";
import { useNodesStatus } from "./nodesStatus";
import { listState, readDense, toSearch, writeDense, type ListState, type NodesSearch } from "./search";
import { ServersToolbar } from "./ServersToolbar";
import { OFFLINE_HINT } from "./useNodeActions";

export interface ServersViewProps {
  search: NodesSearch;
  /** A node's detail shows the list of that node's group, whatever the URL's group says. */
  groupOverride?: GroupKey;
  /** True while a node's detail replaces this list on a phone: the list stays mounted (its state alive) but hidden. */
  hidden?: boolean;
  /** Rendered after the list: the node detail (a sheet on a desktop; nothing extra on a phone, which shows its own page). */
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
function useReorder() {
  const queryClient = useQueryClient();
  const reorderWrite = useApiWrite("reorderNodes");
  const { mutate, isPending } = useMutation({
    mutationKey: REORDER_KEY,
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
  // Reads the group's current order from the cache when a button is pressed (reordering only exists over the whole
  // Servers group in position order, which is the cache's order), so the callback never changes and a poll or a
  // probe result does not hand every row a new one.
  const onMove = useCallback((nodeId: number, delta: -1 | 1) => {
    if (queryClient.isMutating({ mutationKey: REORDER_KEY }) > 0) return;
    const ids = (queryClient.getQueryData<Node[]>(keys.nodes) ?? []).filter((node) => inScope(node, SERVERS)).map((node) => node.id);
    const moved = moveWithin(ids, ids.indexOf(nodeId), delta);
    if (moved) mutate(moved);
  }, [queryClient, mutate]);
  return useMemo(() => ({ busy: isPending, onMove }), [isPending, onMove]);
}

const REORDER_KEY = ["nodes", "reorder"] as const;
/** No health for a sort that does not read it: the rows then keep their order and identity when a probe lands. */
const NO_HEALTH: ReadonlyMap<number, NodeHealth> = new Map();

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
 * Nodes › Servers: the polling owner of nodes, node health and subscriptions — mounted continuously by the /nodes
 * layout, under both the index route and a node's detail, so opening or closing a node never restarts the poll or
 * resets local state (selection, Test all, reorder). The search, sort and group live in the URL; density in
 * localStorage.
 */
export function ServersView({ search, groupOverride, hidden, children }: ServersViewProps) {
  const navigate = useNavigate();
  const desktop = useMediaQuery(DESKTOP_QUERY);
  const { status, statusError } = useNodesStatus();
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
  const sortHealth = sort === "tcp" || sort === "http" ? healthMap : NO_HEALTH;
  const chips = useMemo(() => groupChips(nodes.data ?? [], subs.data ?? []), [nodes.data, subs.data]);
  const shown = useMemo(() => shownNodes(nodes.data ?? [], sortHealth, group, q, sort, dir), [nodes.data, sortHealth, group, q, sort, dir]);
  const rows = visibleRows(shown, showAllGroup === group);
  const reorderControls = useReorder();
  const reorder = canReorder(group, sort, q) ? reorderControls : null;
  const selection = useSelection(group, shown);
  const dialogs = useNodeDialogs();

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
  const activeId = status?.activeId ?? null;
  const activeSince = status?.activeSince ?? null;
  let body: ReactNode;
  if (failed) body = cardFallback([nodes, subs], "Servers did not load");
  else if (!ready) body = <LoadingRows />;
  else if (shown.length === 0) body = <EmptyState title={group === SERVERS ? "No servers here — add one with Add server." : "No servers here"} />;
  else {
    const common = {
      rows, health: healthMap, activeId, activeSince, dense, menu: dialogs.menu, reorder,
      selection: desktop || selecting ? selection.controls : null,
    };
    body = desktop ? <NodeTable {...common} sort={sort} dir={dir} onSort={onSort} /> : <NodeCards {...common} />;
  }

  return (
    <>
      {/* hidden, not omitted: on a phone with a node open, the child below replaces this as the page, but the
          list stays mounted underneath it so its poll, selection and any running Test all survive the visit. */}
      <div className="flex flex-col gap-3" hidden={hidden}>
        {!desktop && ready ? (
          <div className="flex items-center justify-end gap-2">
            {group === SERVERS ? (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button size="icon" aria-label="Add or import servers"><Ellipsis size={16} aria-hidden /></Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent>
                  <DropdownMenuItem onSelect={() => window.setTimeout(dialogs.openAdd, 0)}>Add server</DropdownMenuItem>
                  <DropdownMenuItem onSelect={() => window.setTimeout(dialogs.openImport, 0)}>Import</DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            ) : null}
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
          actions={<GroupActions group={group} shown={shown} nodes={nodes.data} offline={statusError} />}
          manage={desktop ? (group === SERVERS ? (
            <>
              <Button size="sm" onClick={dialogs.openImport}><Upload size={14} aria-hidden />Import</Button>
              <Button size="sm" variant="primary" onClick={dialogs.openAdd}><Plus size={14} aria-hidden />Add server</Button>
            </>
          ) : <span className="text-[11px] text-t3">Add server and Import live in the Servers group</span>) : undefined}
        />
        <FailoverNote lastFailoverAt={status?.lastFailoverAt ?? null} className="px-1" />
        {statusError ? <p className="px-1 text-xs font-semibold text-bad">{OFFLINE_HINT} — connecting is unavailable until it answers.</p> : null}
        {ready ? staleNotice([nodes, health, subs], "Servers did not refresh") : null}
        {body}
        {ready ? <RowCapFooter shown={rows.length} total={shown.length} onShowAll={() => setShowAllGroup(group)} /> : null}
        {selection.nodes.length > 0 ? <BulkBar group={group} selected={selection.nodes} onClear={selection.clear} /> : null}
        <NodeDialogs dialog={dialogs.dialog} onClose={dialogs.close} />
      </div>
      {children}
    </>
  );
}

/**
 * The #/nodes layout route: the Servers list, rendered once and kept mounted under both its index child (no node
 * open) and its $nodeId child (a node's sheet on a desktop or its own page on a phone, rendered through Outlet)
 * — so opening or closing a node never remounts the list. On a phone with a node open, the list stays mounted
 * (its poll and state alive) but hidden, since the child renders a full page of its own there.
 */
export function Servers() {
  const search = useSearch({ from: "/nodes" });
  const desktop = useMediaQuery(DESKTOP_QUERY);
  const { nodeId } = useParams({ strict: false });
  const nodes = useQuery(queries.nodes());   // only to resolve the open node's group; the list below owns the poll
  const openNode = nodeId === undefined ? undefined : nodes.data?.find((node) => String(node.id) === nodeId);
  // On the index route there is no override (the list resolves its own group as before). On the detail route,
  // pass through the open node's group once it is known, else the URL's own group param, else Servers — never
  // resolveGroup's "replace" path, which must not fire while /nodes/$nodeId is open.
  const group = nodeId === undefined ? undefined : (openNode ? groupOf(openNode) : (parseGroup(search.group) ?? SERVERS));
  return (
    <ServersView search={search} groupOverride={group} hidden={!desktop && nodeId !== undefined}>
      <Outlet />
    </ServersView>
  );
}
