import { Link } from "@tanstack/react-router";
import type { Network } from "../../../api/client";
import { CardHeader } from "../../../components/data/CardHeader";
import { EventFeed } from "../../../components/data/EventFeed";
import { GlassCard } from "../../../components/ui/GlassCard";
import { cardFallback, staleNotice, type CardQuery } from "../../../components/data/CardState";
import { clockTime, eventLevel, recentEvents } from "../derive";

/** O11: the last six gateway events, newest first. */
export function EventsCard({ network, className }: { network: CardQuery & { data: Network | undefined }; className?: string }) {
  const fallback = cardFallback([network], "Events did not load", "h-40");
  const header = (
    <CardHeader
      title="Events"
      aside={<Link to="/system/logs" className="text-[11.5px] text-t2 hover:text-t1">View all <span className="text-t3">→ System › Logs</span></Link>}
    />
  );
  if (fallback || !network.data) {
    return <GlassCard aria-label="Events" aria-busy={!network.isError || undefined} className={className}>{header}{fallback}</GlassCard>;
  }
  const items = recentEvents(network.data.events).map((event, index) => ({
    key: `${event.ts}-${index}`, time: clockTime(event.ts), level: eventLevel(event.kind), kind: event.kind, detail: event.detail,
  }));
  return (
    <GlassCard aria-label="Events" className={className}>
      {header}
      {staleNotice([network], "Events did not refresh")}
      <EventFeed items={items} />
    </GlassCard>
  );
}
