import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { ArrowLeft } from "lucide-react";
import { SLOW_POLL_MS } from "../../api/cadence";
import { queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { Sheet, SheetContent } from "../../components/ui/Sheet";
import { DESKTOP_QUERY, useMediaQuery } from "../../lib/media";
import { SERVERS, groupOf, type GroupKey } from "./list";
import { NodeDetailBody } from "./NodeDetail";
import { listState, toSearch, type NodesSearch } from "./search";
import { ServersView } from "./ServersScreen";

interface DetailProps {
  nodeId: number;
  search: NodesSearch;
}

/** Back to the list the detail came from: the node's group, with the list's search and sort. */
function listSearch(search: NodesSearch, group: GroupKey | undefined): NodesSearch {
  return toSearch({ ...listState(search), group });
}

/** Desktop: the Servers list of the node's group stays on screen, the detail in a sheet over it. */
function DetailSheet({ nodeId, search }: DetailProps) {
  const navigate = useNavigate();
  const nodes = useQuery(queries.nodes());   // the list behind polls it
  const node = nodes.data?.find((item) => item.id === nodeId);
  const group = node ? groupOf(node) : undefined;
  const title = node?.name ?? (nodes.data ? "Node not found" : "Node");
  return (
    <ServersView search={search} groupOverride={group}>
      <Sheet open onOpenChange={(open) => { if (!open) void navigate({ to: "/nodes", search: listSearch(search, group) }); }}>
        <SheetContent title={title}>
          <NodeDetailBody nodeId={nodeId} showName={false} />
        </SheetContent>
      </Sheet>
    </ServersView>
  );
}

/** Phone: a page of its own, the polling owner of nodes and node health, with a way back to its group. */
function DetailPage({ nodeId, search }: DetailProps) {
  const nodes = usePolledQuery(queries.nodes(), SLOW_POLL_MS);
  usePolledQuery(queries.nodeHealth(), SLOW_POLL_MS);
  const subs = useQuery(queries.subs());   // the group's name only; read once
  const node = nodes.data?.find((item) => item.id === nodeId);
  const group = node ? groupOf(node) : undefined;
  const groupName = group === undefined ? null : group === SERVERS ? "Servers" : subs.data?.find((sub) => sub.id === group)?.name;
  return (
    <div className="flex flex-col gap-3">
      <Link to="/nodes" search={listSearch(search, group)} className="inline-flex min-h-11 items-center gap-1.5 self-start text-sm font-semibold text-t2 hover:text-t1">
        <ArrowLeft size={16} aria-hidden />
        Servers{groupName ? ` · ${groupName}` : ""}
      </Link>
      <NodeDetailBody nodeId={nodeId} showName />
    </div>
  );
}

/** The #/nodes/$nodeId route: a sheet over the list from 768 px, a page below. */
export function NodeDetail() {
  const { nodeId } = useParams({ from: "/nodes/$nodeId" });
  const search = useSearch({ from: "/nodes/$nodeId" });
  const desktop = useMediaQuery(DESKTOP_QUERY);
  const id = /^\d+$/.test(nodeId) ? Number(nodeId) : Number.NaN;
  return desktop ? <DetailSheet nodeId={id} search={search} /> : <DetailPage nodeId={id} search={search} />;
}
