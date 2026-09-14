import { useQuery } from "@tanstack/react-query";
import { queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { NETWORK_POLL_MS, SLOW_POLL_MS } from "./cadence";
import { activeNode } from "./derive";
import { Alerts } from "./overview/Alerts";
import { KpiGrid } from "./overview/KpiGrid";
import { StatusBlock } from "./overview/StatusBlock";

/** Home › Overview. This page is the one polling owner of every read it shows except `status` (the shell's). */
export function Overview() {
  const status = useQuery(queries.status());
  const network = usePolledQuery(queries.network(), NETWORK_POLL_MS);
  const nodes = usePolledQuery(queries.nodes(), SLOW_POLL_MS);
  usePolledQuery(queries.nodeHealth(), SLOW_POLL_MS);
  const subs = usePolledQuery(queries.subs(), SLOW_POLL_MS);
  usePolledQuery(queries.routing(), SLOW_POLL_MS);
  const activeName = activeNode(nodes.data, status.data?.active_node_id)?.name ?? null;

  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-12">
      <Alerts status={status.data} subs={subs.data} activeName={activeName} className="md:col-span-2 xl:col-span-12" />
      <StatusBlock status={status.data} statusError={status.isError} network={network.data} nodes={nodes.data} className="md:col-span-2 xl:col-span-5" />
      <KpiGrid className="md:col-span-2 xl:col-span-7" />
    </div>
  );
}
