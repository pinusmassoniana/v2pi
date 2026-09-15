import { Link } from "@tanstack/react-router";
import { memo } from "react";
import type { Node, NodeHealth } from "../../api/client";
import { serverNow } from "../../api/clock";
import { Uptime } from "../../components/data/Uptime";
import { cn } from "../../lib/cn";
import { flagEmoji } from "../../lib/flag";
import { bestReading, checkedAgo } from "./list";
import type { NodeListProps } from "./NodeTable";
import { NodeRowActions } from "./NodeRowActions";
import { ActiveRealPill, FailBadge, ProbePill, StaleBadge } from "./probe";
import type { NodesSearch } from "./search";

interface CardProps {
  node: Node;
  health: NodeHealth | undefined;
  active: boolean;
  activeSince: number | null;
  dense: boolean;
  detailSearch: NodesSearch;
}

/** "real 42 ms · 5 s ago", "checked 12 min ago", or "not probed". */
function readingLine(health: NodeHealth | undefined): string {
  const ago = checkedAgo(health?.checked_at, serverNow());
  if (!ago) return "not probed";
  const reading = bestReading(health);
  return reading ? `${reading.label} ${reading.ms} ms · ${ago}` : `checked ${ago}`;
}

/**
 * A card is a link to the node's page with its own buttons on top: the link covers the card, the content lets
 * taps through, and only the buttons take them back.
 */
const NodeCard = memo(function NodeCard({ node, health, active, activeSince, dense, detailSearch }: CardProps) {
  const flag = flagEmoji(health?.egress_cc);
  const failCount = active ? (health?.fail_count ?? 0) : 0;
  return (
    <li
      data-node-id={node.id}
      data-node-name={node.name}
      data-active={active || undefined}
      data-stale={node.stale || undefined}
      className={cn("glass relative", dense ? "p-2.5" : "p-3.5", node.stale && "opacity-55", active && "shadow-[0_0_0_2px_var(--g2)]")}
    >
      <Link
        to="/nodes/$nodeId"
        params={{ nodeId: String(node.id) }}
        search={detailSearch}
        aria-label={node.name}
        className="absolute inset-0 rounded-[18px] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-g2"
      />
      <div className="pointer-events-none relative flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <p className="flex min-w-0 items-center gap-1.5 text-sm font-semibold text-t1">
            <span className="truncate">{flag ? `${flag} ` : ""}{node.name}</span>
            {node.stale ? <StaleBadge /> : null}
            {failCount > 0 ? <FailBadge count={failCount} /> : null}
          </p>
          {active ? (
            <p className="text-[11px] font-semibold text-ok">connected{activeSince !== null ? <> · <Uptime since={activeSince} running coarse /></> : null}</p>
          ) : null}
          <div className="mt-1.5 flex flex-wrap gap-1">
            <ProbePill label="TCP" ok={health?.last_tcp_ok} ms={health?.last_tcp_ms} />
            <ProbePill label="HTTP" ok={health?.last_http_ok} ms={health?.last_http_ms} />
            {active ? <ActiveRealPill label="real" nodeId={node.id} health={health} /> : <ProbePill label="real" ok={health?.last_real_ok} ms={health?.last_real_ms} />}
          </div>
          <p className="mt-1 truncate text-[11px] text-t3">{readingLine(health)}</p>
        </div>
        <NodeRowActions node={node} active={active} withMenu={false} className="pointer-events-auto" />
      </div>
    </li>
  );
});

/** N5 on a phone: one card per node. */
export function NodeCards({ rows, health, activeId, activeSince, dense, detailSearch }: NodeListProps) {
  return (
    <ul aria-label="Nodes" data-dense={dense || undefined} className="flex flex-col gap-2">
      {rows.map((node) => (
        <NodeCard
          key={node.id}
          node={node}
          health={health.get(node.id)}
          active={node.id === activeId}
          activeSince={node.id === activeId ? activeSince : null}
          dense={dense}
          detailSearch={detailSearch}
        />
      ))}
    </ul>
  );
}
