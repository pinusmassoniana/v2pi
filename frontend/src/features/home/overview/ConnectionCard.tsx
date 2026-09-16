import type { Network, Node, Status } from "../../../api/client";
import { useTraffic } from "../../../api/traffic";
import { CardHeader } from "../../../components/data/CardHeader";
import { ConnectionPath } from "../../../components/data/ConnectionPath";
import { GlassCard } from "../../../components/ui/GlassCard";
import { cardFallback, staleNotice, type CardQuery } from "../../../components/data/CardState";
import { splitLeadingFlag } from "../../../lib/flag";
import { killSwitchState } from "../../../lib/network";
import { connectedState } from "../../../lib/nodeHealth";
import {
  activeFlag, activeNodeLabel, nodeHealthSlot, outboundRates, pathLabel, poolSize, probeFor, rateLines,
} from "../derive";
import { LiveChip } from "./LiveChip";

export interface ConnectionCardProps {
  status: Status | undefined;
  /** The status poll is failing: the connected time is not known. */
  statusError: boolean;
  network: CardQuery & { data: Network | undefined };
  nodes: Node[] | undefined;
  className?: string;
}

/** O8: devices → gateway → node → internet, from the network read, the status poll and the live traffic frame. */
export function ConnectionCard({ status, statusError, network, nodes, className }: ConnectionCardProps) {
  const traffic = useTraffic();
  const fallback = cardFallback([network], "Network status did not load", "h-48");
  if (fallback || !network.data) {
    return (
      <GlassCard aria-label="Connection path" aria-busy={!network.isError || undefined} className={className}>
        <CardHeader title="Connection path" />
        {fallback}
      </GlassCard>
    );
  }

  const net = network.data;
  const frame = traffic.disabled ? null : traffic.live;
  const activeId = status?.active_node_id ?? null;
  const probe = probeFor(frame, activeId);
  // The name without its own flag; the marker shows the egress flag, else the one the name started with.
  const named = splitLeadingFlag(activeNodeLabel(status, nodes));
  const slot = nodeHealthSlot(status, frame, probe);
  const rates = outboundRates(frame);
  const kill = killSwitchState(net);
  const dim = !traffic.disabled && !traffic.fresh;
  // The rule Nodes uses: connected while the status poll answers and xray runs, with a node active.
  const since = activeId !== null && connectedState(status, statusError) === "connected" ? (status?.active_since ?? null) : null;
  return (
    <GlassCard aria-label="Connection path" className={className}>
      <CardHeader title="Connection path" aside={<LiveChip disabled={traffic.disabled} fresh={traffic.fresh} />} />
      {staleNotice([network], "Network status did not refresh")}
      <ConnectionPath
        label={pathLabel({ name: named.name, slot, rates, killSwitch: kill.label, dim })}
        clients={net.status.dhcp_clients}
        poolSize={poolSize(net.segment)}
        gatewayIp={net.segment.ip || null}
        gatewayIface={net.segment.iface || null}
        nodeName={named.name}
        nodeFlag={activeId === null ? "" : activeFlag(probe) || named.flag}
        node={slot}
        connectedSince={since}
        egressIp={probe?.egress_ip ?? null}
        egressIp6={probe?.egress_ip6 ?? null}
        uplink={net.status.uplink}
        uplink6={net.status.uplink6}
        ipv6Enabled={net.ipv6_enabled}
        rates={rates}
        tunnelLines={rateLines(rates?.proxy ?? null)}
        directLines={rateLines(rates?.direct ?? null)}
        killSwitch={kill}
        dim={dim}
      />
    </GlassCard>
  );
}
