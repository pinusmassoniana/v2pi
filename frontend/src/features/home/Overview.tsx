import { useQuery } from "@tanstack/react-query";
import { queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { XrayCard } from "../../app/shell/XrayCard";
import { NETWORK_POLL_MS, SLOW_POLL_MS } from "../../api/cadence";
import { activeNodeLabel } from "./derive";
import { Alerts } from "./overview/Alerts";
import { ConnectionCard } from "./overview/ConnectionCard";
import { EventsCard } from "./overview/EventsCard";
import { KpiGrid } from "./overview/KpiGrid";
import { NetworkCard } from "./overview/NetworkCard";
import { RoutingCard } from "./overview/RoutingCard";
import { StatusBlock } from "./overview/StatusBlock";
import { UpstreamHealthCard } from "./overview/UpstreamHealthCard";
import { OverviewThroughput } from "./ThroughputCard";
import { useRefreshOnSwitch } from "./useRefreshOnSwitch";

/**
 * Home › Overview. This page is the one polling owner of every read it shows except `status`, which the
 * shell polls. On a phone the grid collapses to one column in DOM order: alerts, status, KPIs, traffic,
 * upstream health, connection path, events, routing, network, and the xray-core switch last.
 */
export function Overview() {
  const status = useQuery(queries.status());
  const network = usePolledQuery(queries.network(), NETWORK_POLL_MS);
  const nodes = usePolledQuery(queries.nodes(), SLOW_POLL_MS);
  const health = usePolledQuery(queries.nodeHealth(), SLOW_POLL_MS);
  const subs = usePolledQuery(queries.subs(), SLOW_POLL_MS);
  const routing = usePolledQuery(queries.routing(), SLOW_POLL_MS);
  useRefreshOnSwitch(status.data);
  const activeLabel = activeNodeLabel(status.data, nodes.data);

  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-12">
      <Alerts status={status.data} subs={subs.data} activeLabel={activeLabel} className="md:col-span-2 xl:col-span-12" />
      <StatusBlock status={status.data} statusError={status.isError} network={network.data} nodes={nodes.data} className="md:col-span-2 xl:col-span-5" />
      <KpiGrid className="md:col-span-2 xl:col-span-7" />
      <OverviewThroughput className="md:col-span-2 xl:col-span-8" />
      <UpstreamHealthCard status={status.data} statusError={status.isError} nodes={nodes} health={health} className="md:col-span-2 xl:col-span-4" />
      <ConnectionCard status={status.data} network={network} nodes={nodes.data} className="xl:col-span-6" />
      <EventsCard network={network} className="xl:col-span-6" />
      <RoutingCard routing={routing} activeLabel={activeLabel} className="xl:col-span-6" />
      <NetworkCard network={network} className="xl:col-span-6" />
      <XrayCard status={status.data} className="md:hidden" />
    </div>
  );
}
