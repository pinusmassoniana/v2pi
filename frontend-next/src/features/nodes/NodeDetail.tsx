import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { Activity, CopyPlus, Pencil, Share2, Trash2, Unlink } from "lucide-react";
import type { ReactNode } from "react";
import type { Node } from "../../api/client";
import { queries } from "../../api/keys";
import { Button } from "../../components/ui/Button";
import { EmptyState, ErrorState, Skeleton } from "../../components/ui/States";
import { flagEmoji } from "../../lib/flag";
import { connectedState } from "../../lib/nodeHealth";
import { SERVERS, groupOf } from "./list";
import { NodeConfigCard } from "./NodeConfigCard";
import { NodeHealthCard } from "./NodeHealthCard";
import { DISCONNECT_FIRST, NO_MENU_CALLBACKS, type NodeMenuCallbacks } from "./NodeRowActions";
import { useNodesStatus } from "./nodesStatus";
import { ActiveLine, FailBadge, StaleBadge } from "./probe";
import { ProfileRow } from "./ProfileRow";
import { toSearch } from "./search";
import { useNodeConnection, useNodeRemoval, useNodeTest } from "./useNodeActions";

/** A control with the reason it is disabled written under it, so the reason is visible, not only a tooltip. */
function WithHint({ hint, children }: { hint: string | null; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      {children}
      {hint ? <span className="text-[11px] text-t3">{hint}</span> : null}
    </div>
  );
}

function PrimaryActions({ node, active }: { node: Node; active: boolean }) {
  const connection = useNodeConnection(node, active);
  const test = useNodeTest(node);
  return (
    <div className="grid grid-cols-2 gap-2">
      <WithHint hint={connection.reason}>
        <Button variant={active ? "danger" : "primary"} disabled={connection.disabled} onClick={connection.toggle} className="w-full">
          {connection.label}
        </Button>
      </WithHint>
      <Button disabled={test.busy} onClick={test.run} className="w-full">
        <Activity size={15} aria-hidden className={test.busy ? "animate-pulse" : undefined} />
        {test.busy ? "Testing…" : "Test"}
      </Button>
    </div>
  );
}

function SecondaryActions({ node, active, menu }: { node: Node; active: boolean; menu: NodeMenuCallbacks }) {
  const navigate = useNavigate();
  const removal = useNodeRemoval();
  const manual = node.subscription_id === null;
  const { onEdit, onClone, onExport } = menu;
  return (
    <section aria-label="Node actions" className="flex flex-wrap items-start gap-2">
      {onEdit ? (
        <WithHint hint={active ? DISCONNECT_FIRST : null}>
          <Button size="sm" disabled={active} onClick={() => onEdit(node)}><Pencil size={14} aria-hidden />Edit</Button>
        </WithHint>
      ) : null}
      {onClone ? <Button size="sm" onClick={() => onClone(node)}><CopyPlus size={14} aria-hidden />Clone</Button> : null}
      {onExport ? <Button size="sm" onClick={() => onExport(node)}><Share2 size={14} aria-hidden />Export</Button> : null}
      {manual ? (
        <WithHint hint={active ? DISCONNECT_FIRST : null}>
          <Button
            size="sm"
            variant="danger"
            disabled={active || removal.busy}
            onClick={async () => {
              if (await removal.deleteNode(node)) void navigate({ to: "/nodes", search: { group: SERVERS } });
            }}
          >
            <Trash2 size={14} aria-hidden />Delete
          </Button>
        </WithHint>
      ) : (
        <Button size="sm" disabled={removal.busy} onClick={() => void removal.detachNode(node)}>
          <Unlink size={14} aria-hidden />Detach
        </Button>
      )}
    </section>
  );
}

export interface NodeDetailBodyProps {
  nodeId: number;
  /** The page shows the node's name as its own heading; a sheet already titles itself with it. */
  showName: boolean;
  menu?: NodeMenuCallbacks;
}

/**
 * N5–N7 and N13–N16 for one node: the header, Connect / Test, health, config, tuning profile and the rest of its
 * actions. Reads every query from the cache; the Servers list (desktop) or the page (phone) keeps them fresh.
 */
export function NodeDetailBody({ nodeId, showName, menu = NO_MENU_CALLBACKS }: NodeDetailBodyProps) {
  const { status, statusError } = useNodesStatus();
  const nodes = useQuery(queries.nodes());
  const health = useQuery(queries.nodeHealth());
  const subs = useQuery(queries.subs());
  const node = nodes.data?.find((item) => item.id === nodeId);

  if (!node) {
    if (nodes.isError && nodes.data === undefined) return <ErrorState message="Node did not load" onRetry={() => void nodes.refetch()} />;
    if (nodes.data === undefined) return <Skeleton className="h-64" />;
    return (
      <EmptyState title="Node not found">
        <Link to="/nodes" className="font-semibold text-t1 underline">Back to Servers</Link>
      </EmptyState>
    );
  }

  const active = status?.activeId === node.id;
  const nodeHealth = health.data?.find((row) => row.node_id === node.id);
  const group = groupOf(node);
  const groupName = group === SERVERS ? "Servers" : (subs.data?.find((sub) => sub.id === group)?.name ?? `subscription #${group}`);
  const flag = flagEmoji(nodeHealth?.egress_cc);
  const failCount = active ? (nodeHealth?.fail_count ?? 0) : 0;
  const since = status?.activeSince ?? null;

  return (
    <div className="flex flex-col gap-3">
      <header className="flex flex-col gap-1.5">
        {showName ? <h2 className="truncate text-lg font-bold tracking-tight text-t1">{flag ? `${flag} ` : ""}{node.name}</h2> : null}
        <div className="flex flex-wrap items-center gap-1.5 text-[11.5px] text-t2">
          <Link to="/nodes" search={toSearch({ group, q: "", sort: "pos", dir: "asc" })} className="rounded-full border border-line px-2 py-0.5 font-semibold text-t1 hover:bg-glass-2">
            {groupName}
          </Link>
          {!showName && flag ? <span aria-label={`egress ${nodeHealth?.egress_cc}`}>{flag}</span> : null}
          {node.stale ? <StaleBadge /> : null}
          {failCount > 0 ? <FailBadge count={failCount} /> : null}
          <span className="font-mono text-t3">id {node.id}</span>
          <span>· {node.transport} · {node.security}</span>
        </div>
        {active ? <ActiveLine state={connectedState(status, statusError)} since={since} className="text-xs" /> : null}
      </header>
      <PrimaryActions node={node} active={active} />
      <NodeHealthCard node={node} health={nodeHealth} active={active} />
      <NodeConfigCard node={node} />
      <ProfileRow node={node} active={active} />
      <SecondaryActions node={node} active={active} menu={menu} />
    </div>
  );
}
