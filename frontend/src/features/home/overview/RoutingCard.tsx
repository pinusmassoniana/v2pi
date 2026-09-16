import { Link } from "@tanstack/react-router";
import type { Routing } from "../../../api/client";
import { CardHeader } from "../../../components/data/CardHeader";
import { GlassCard } from "../../../components/ui/GlassCard";
import { cn } from "../../../lib/cn";
import { cardFallback, staleNotice, type CardQuery } from "../../../components/data/CardState";
import { routingSummary, type RouteBadge } from "../derive";

const BADGE: Record<RouteBadge, string> = {
  proxy: "bg-g2/20 text-g2",
  direct: "bg-glass-2 text-t2",
  block: "bg-bad/15 text-bad",
};

function Badge({ badge }: { badge: RouteBadge }) {
  return <span data-badge={badge} className={cn("w-[50px] shrink-0 rounded-md py-0.5 text-center text-[10.5px] font-bold", BADGE[badge])}>{badge}</span>;
}

/** O9: the first four enabled rules and where everything else goes. */
export function RoutingCard({ routing, activeLabel, className }: { routing: CardQuery & { data: Routing | undefined }; activeLabel: string; className?: string }) {
  const fallback = cardFallback([routing], "Routing did not load", "h-40");
  const link = <Link to="/tunnel/routing" className="text-[11.5px] text-t2 hover:text-t1">Tunnel › Routing <span className="text-t3">→</span></Link>;
  if (fallback || !routing.data) {
    return (
      <GlassCard aria-label="Routing" aria-busy={!routing.isError || undefined} className={className}>
        <CardHeader title="Routing" aside={link} />
        {fallback}
      </GlassCard>
    );
  }
  const summary = routingSummary(routing.data, activeLabel);
  return (
    <GlassCard aria-label="Routing" className={className}>
      <CardHeader title="Routing" detail={`· ${summary.header}`} aside={link} />
      {staleNotice([routing], "Routing did not refresh")}
      <ul className="flex flex-col gap-1">
        {summary.rows.length === 0 ? <li className="py-1 text-sm text-t3">No enabled rules.</li> : null}
        {summary.rows.map((row) => (
          <li key={row.id} className="flex items-center gap-2.5 py-1 text-xs">
            <Badge badge={row.badge} />
            <span className="truncate font-mono text-t1">{row.text}</span>
          </li>
        ))}
        <li data-default className="mt-0.5 flex items-center gap-2.5 rounded-xl border border-line bg-glass px-2.5 py-2 text-xs">
          <span className="text-t3">default:</span>
          <Badge badge={summary.defaultBadge} />
          {summary.defaultTarget ? <span className="truncate text-t1">{summary.defaultTarget}</span> : null}
        </li>
      </ul>
    </GlassCard>
  );
}
