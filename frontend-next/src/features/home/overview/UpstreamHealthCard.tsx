import { Link } from "@tanstack/react-router";
import type { Node, NodeHealth, Status } from "../../../api/client";
import { serverNow } from "../../../api/clock";
import { useTraffic } from "../../../api/traffic";
import { CardHeader } from "../../../components/data/CardHeader";
import { LatencyBars } from "../../../components/data/LatencyBars";
import { GlassCard } from "../../../components/ui/GlassCard";
import { Pill } from "../../../components/ui/Pill";
import { cardFallback, staleNotice, type CardQuery } from "../../../components/data/CardState";
import { activeNode, activeRow, failoverPill, probeFor, standbyRows, tunnelLabel } from "../derive";

export interface UpstreamHealthCardProps {
  status: Status | undefined;
  statusError: boolean;
  nodes: CardQuery & { data: Node[] | undefined };
  health: CardQuery & { data: NodeHealth[] | undefined };
  className?: string;
}

const SUBHEAD = "text-[9.5px] font-semibold uppercase tracking-[.07em] text-t3";

/** O7: the active node's live probe, up to four standbys from the background sweep, and whether failover can happen. */
export function UpstreamHealthCard({ status, statusError, nodes, health, className }: UpstreamHealthCardProps) {
  const traffic = useTraffic();
  const queries = [nodes, health];
  const fallback = cardFallback(queries, "Node health did not load", "h-44");
  const activeId = status?.active_node_id ?? null;
  const probe = probeFor(traffic.disabled ? null : traffic.live, activeId);
  const pill = failoverPill(status, tunnelLabel(status, statusError, traffic.fresh ? probe : null));
  const header = <CardHeader title="Upstream health" aside={<Pill tone={pill.tone} dot>{pill.label}</Pill>} />;

  if (fallback) {
    return <GlassCard aria-label="Upstream health" aria-busy={!queries.some((q) => q.isError) || undefined} className={className}>{header}{fallback}</GlassCard>;
  }

  const allNodes = nodes.data ?? [];
  const now = serverNow();
  const active = activeRow(activeNode(allNodes, activeId), probe, !traffic.disabled && !traffic.fresh);
  const standby = standbyRows(allNodes, health.data ?? [], activeId, now);

  return (
    <GlassCard aria-label="Upstream health" className={className}>
      {header}
      {staleNotice(queries, "Node health did not refresh")}
      {allNodes.length === 0 ? (
        <p className="py-2 text-sm text-t3">
          No nodes yet — add one in <Link to="/nodes" className="font-semibold text-t1 underline">Nodes › Servers</Link>.
        </p>
      ) : (
        <>
          {active ? (
            <>
              <p className={`${SUBHEAD} mb-0.5 mt-1.5`}>Active · live probe</p>
              <LatencyBars rows={[active]} label="Active node" ticks={[]} />
            </>
          ) : null}
          {standby.length > 0 ? (
            <>
              <p className={`${SUBHEAD} mt-2`}>Standby · background sweep</p>
              <LatencyBars rows={standby} label="Standby nodes" />
            </>
          ) : null}
        </>
      )}
    </GlassCard>
  );
}
