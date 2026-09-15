import { useQuery } from "@tanstack/react-query";
import { useNavigate, useRouterState, useSearch } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { SLOW_POLL_MS } from "../../api/cadence";
import { queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { cardFallback, staleNotice } from "../../components/data/CardState";
import { EmptyState, Skeleton } from "../../components/ui/States";
import { FailoverNote } from "./FailoverNote";
import { SERVERS, groupChips, healthById, resolveGroup, shownNodes, visibleRows, type GroupKey } from "./list";
import { listState, readDense, toSearch, writeDense, type ListState, type NodesSearch } from "./search";
import { ServersToolbar } from "./ServersToolbar";

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
 * Nodes › Servers: the polling owner of nodes, node health and subscriptions while it is mounted (the detail route
 * mounts it too, on a desktop). The search, sort and group live in the URL; density in localStorage.
 */
export function ServersView({ search, groupOverride, children }: ServersViewProps) {
  const navigate = useNavigate();
  const status = useQuery(queries.status());
  const nodes = usePolledQuery(queries.nodes(), SLOW_POLL_MS);
  const health = usePolledQuery(queries.nodeHealth(), SLOW_POLL_MS);
  const subs = usePolledQuery(queries.subs(), SLOW_POLL_MS);
  const entry = useRouterState({ select: (state) => state.location.state.__TSR_index });
  const [dense, setDense] = useState(readDense);

  const list: ListState = { ...listState(search), group: groupOverride ?? search.group };
  const resolved = groupOverride !== undefined ? { group: groupOverride, replace: false } : resolveGroup(search.group, subs.data);
  const group = resolved.group;
  const healthMap = useMemo(() => healthById(health.data), [health.data]);
  const chips = useMemo(() => groupChips(nodes.data ?? [], subs.data ?? []), [nodes.data, subs.data]);
  const shown = useMemo(
    () => shownNodes(nodes.data ?? [], healthMap, group, list.q, list.sort, list.dir),
    [nodes.data, healthMap, group, list.q, list.sort, list.dir],
  );
  const rows = visibleRows(shown, false);

  // N1: a group that does not exist (any more) falls back to the default, without a history entry of its own.
  useEffect(() => {
    if (resolved.replace) void navigate({ to: "/nodes", search: (prev) => ({ ...prev, group: undefined }), replace: true });
  }, [resolved.replace, navigate]);

  const { q, sort, dir } = list;
  const listGroup = list.group;
  const onList = useCallback(
    (patch: Partial<ListState>) => void navigate({ to: "/nodes", search: toSearch({ group: listGroup, q, sort, dir, ...patch }), replace: true }),
    [navigate, listGroup, q, sort, dir],
  );
  const onDense = useCallback((next: boolean) => {
    setDense(next);
    writeDense(next);
  }, []);

  const ready = nodes.data !== undefined && subs.data !== undefined;
  const failed = (nodes.isError && nodes.data === undefined) || (subs.isError && subs.data === undefined);
  let body: ReactNode;
  if (failed) body = cardFallback([nodes, subs], "Servers did not load");
  else if (!ready) body = <LoadingRows />;
  else if (shown.length === 0) body = <EmptyState title={group === SERVERS ? "No servers here — add one with Add server." : "No servers here"} />;
  else {
    body = (
      <ul aria-label="Nodes" data-dense={dense || undefined} className="glass flex flex-col p-2">
        {rows.map((node) => <li key={node.id} data-node-id={node.id} data-node-name={node.name} className="px-2 py-1.5 text-sm">{node.name}</li>)}
      </ul>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <ServersToolbar chips={chips} group={group} list={list} onList={onList} entry={entry} dense={dense} onDense={onDense} />
      <FailoverNote status={status.data} className="px-1" />
      {ready ? staleNotice([nodes, health, subs], "Servers did not refresh") : null}
      {body}
      {children}
    </div>
  );
}

/** The #/nodes route. */
export function Servers() {
  return <ServersView search={useSearch({ from: "/nodes" })} />;
}
