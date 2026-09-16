import { Link } from "@tanstack/react-router";
import type { Network } from "../../../api/client";
import { CardHeader } from "../../../components/data/CardHeader";
import { KeyValueRows } from "../../../components/data/KeyValueRows";
import { GlassCard } from "../../../components/ui/GlassCard";
import { cardFallback, staleNotice, type CardQuery } from "../../../components/data/CardState";
import { ipv6Source } from "../../../lib/network";
import { poolSize } from "../derive";

/** O10: the client segment, its DHCP pool, the DNS handed out, and where IPv6 comes from. */
export function NetworkCard({ network, className }: { network: CardQuery & { data: Network | undefined }; className?: string }) {
  const fallback = cardFallback([network], "Network settings did not load", "h-32");
  const header = <CardHeader title="Network" aside={<Link to="/gateway/network" className="text-[11.5px] text-t2 hover:text-t1">Gateway › Network <span className="text-t3">→</span></Link>} />;
  if (fallback || !network.data) {
    return <GlassCard aria-label="Network" aria-busy={!network.isError || undefined} className={className}>{header}{fallback}</GlassCard>;
  }
  const { segment, status } = network.data;
  const pool = poolSize(segment);
  return (
    <GlassCard aria-label="Network" className={className}>
      {header}
      {staleNotice([network], "Network settings did not refresh")}
      <KeyValueRows
        rows={[
          { key: "Segment", value: <span className="font-mono">{segment.ip || "—"}{segment.iface ? ` · ${segment.iface}` : ""}</span> },
          {
            key: "DHCP pool",
            value: <span className="font-mono">{segment.dhcp_start || "—"}–{segment.dhcp_end || "—"}</span>,
            sub: `${status.dhcp_clients} clients · pool ${pool ?? "—"}`,
          },
          { key: "Client DNS", value: <span className="font-mono">{segment.client_dns || "—"}</span> },
          { key: "IPv6 source", value: ipv6Source(network.data) },
        ]}
      />
    </GlassCard>
  );
}
