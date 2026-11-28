import { Link } from "@tanstack/react-router";
import type { FailoverState, Node, NodeHealth, Status, Subscription } from "../../../api/client";
import { serverNow } from "../../../api/clock";
import { CardHeader } from "../../../components/data/CardHeader";
import { GlassCard } from "../../../components/ui/GlassCard";
import { cardFallback, staleNotice, type CardQuery } from "../../../components/data/CardState";
import { FailoverListView } from "../../tunnel/FailoverScreen";
import { failoverRows } from "../../tunnel/failoverList";
import { UpstreamHealthCard } from "./UpstreamHealthCard";

export interface FailoverCardProps {
  status: Status | undefined;
  statusError: boolean;
  failover: CardQuery & { data: FailoverState | undefined };
  nodes: CardQuery & { data: Node[] | undefined };
  health: CardQuery & { data: NodeHealth[] | undefined };
  subs: CardQuery & { data: Subscription[] | undefined };
  className?: string;
}

/** The failover list at a glance; with no list set up, the upstream-health view it replaces. */
export function FailoverCard({ status, statusError, failover, nodes, health, subs, className }: FailoverCardProps) {
  if (!failover.data?.order.length) {
    return <UpstreamHealthCard status={status} statusError={statusError} nodes={nodes} health={health} className={className} />;
  }
  const queries = [failover, nodes, health];
  const header = <CardHeader title="Failover list" aside={<Link to="/tunnel/failover" className="text-caption font-semibold text-t2 underline">edit</Link>} />;
  const fallback = cardFallback(queries, "The failover list did not load", "h-44");
  const rows = failoverRows(failover.data, nodes.data ?? [], health.data ?? [], subs.data ?? [], status?.active_node_id ?? null, serverNow());
  return (
    <GlassCard aria-label="Failover list" className={className}>
      {header}
      {fallback ?? (
        <>
          {staleNotice(queries, "The failover list did not refresh")}
          <FailoverListView rows={rows} />
        </>
      )}
    </GlassCard>
  );
}
