import type { Network, Node, Status } from "../../../api/client";
import { useTraffic } from "../../../api/traffic";
import { CardHeader } from "../../../components/data/CardHeader";
import { ConnectionPath } from "../../../components/data/ConnectionPath";
import { GlassCard } from "../../../components/ui/GlassCard";
import { cardFallback, staleNotice, type CardQuery } from "../../../components/data/CardState";
import { killSwitchState } from "../../../lib/network";
import { activeFlag, activeNodeLabel, bypassState, liveLatency, poolSize, probeFor, tunnelLeg } from "../derive";

export interface ConnectionCardProps {
  status: Status | undefined;
  network: CardQuery & { data: Network | undefined };
  nodes: Node[] | undefined;
  className?: string;
}

/** O8: devices → gateway → node → internet, from the network read and the live traffic frame. */
export function ConnectionCard({ status, network, nodes, className }: ConnectionCardProps) {
  const traffic = useTraffic();
  const fallback = cardFallback([network], "Network status did not load", "h-48");
  const header = <CardHeader title="Connection path" />;
  if (fallback || !network.data) {
    return <GlassCard aria-label="Connection path" aria-busy={!network.isError || undefined} className={className}>{header}{fallback}</GlassCard>;
  }

  const net = network.data;
  const frame = traffic.disabled ? null : traffic.live;
  const activeId = status?.active_node_id ?? null;
  const probe = probeFor(frame, activeId);
  return (
    <GlassCard aria-label="Connection path" className={className}>
      {header}
      {staleNotice([network], "Network status did not refresh")}
      <ConnectionPath
        clients={net.status.dhcp_clients}
        poolSize={poolSize(net.segment)}
        gatewayIp={net.segment.ip || null}
        gatewayIface={net.segment.iface || null}
        nodeName={activeNodeLabel(status, nodes)}
        nodeFlag={activeFlag(probe)}
        latencyMs={liveLatency(probe)}
        egressIp={probe?.egress_ip ?? null}
        egressIp6={probe?.egress_ip6 ?? null}
        uplink={net.status.uplink}
        uplink6={net.status.uplink6}
        ipv6Enabled={net.ipv6_enabled}
        leg={tunnelLeg(status, probe)}
        bypassBps={bypassState(frame).total}
        killSwitch={killSwitchState(net)}
        dim={!traffic.disabled && !traffic.fresh}
      />
    </GlassCard>
  );
}
