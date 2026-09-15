import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { ArrowLeft } from "lucide-react";
import { queries } from "../../api/keys";
import { Sheet, SheetContent } from "../../components/ui/Sheet";
import { DESKTOP_QUERY, useMediaQuery } from "../../lib/media";
import { SERVERS, groupOf, type GroupKey } from "./list";
import { NodeDetailBody } from "./NodeDetail";
import { NodeDialogs, useNodeDialogs } from "./NodeDialogs";
import { listState, toSearch, type NodesSearch } from "./search";

interface DetailProps {
  nodeId: number;
  search: NodesSearch;
}

/** Back to the list the detail came from: the node's group, with the list's search and sort. */
function listSearch(search: NodesSearch, group: GroupKey | undefined): NodesSearch {
  return toSearch({ ...listState(search), group });
}

/**
 * Desktop: a Sheet over the Servers list, which the /nodes layout keeps mounted underneath (showing this node's
 * group already, via its own groupOverride) — closing it never unmounts or resets that list.
 */
function DetailSheet({ nodeId, search }: DetailProps) {
  const navigate = useNavigate();
  const nodes = useQuery(queries.nodes());   // the list underneath already polls it; this only reads the title/group
  const node = nodes.data?.find((item) => item.id === nodeId);
  const group = node ? groupOf(node) : undefined;
  const title = node?.name ?? (nodes.data ? "Node not found" : "Node");
  const dialogs = useNodeDialogs();
  return (
    <>
      {/* Closing replaces the node's history entry, so Back does not open the sheet again. */}
      <Sheet open onOpenChange={(open) => { if (!open) void navigate({ to: "/nodes", search: listSearch(search, group), replace: true }); }}>
        <SheetContent title={title}>
          <NodeDetailBody nodeId={nodeId} showName={false} menu={dialogs.menu} />
        </SheetContent>
      </Sheet>
      <NodeDialogs dialog={dialogs.dialog} onClose={dialogs.close} />
    </>
  );
}

/**
 * Phone: a page of its own. The /nodes layout's Servers list keeps polling nodes and node health underneath it
 * (hidden, not unmounted), so this reads them from the cache instead of polling a second time.
 */
function DetailPage({ nodeId, search }: DetailProps) {
  const nodes = useQuery(queries.nodes());
  const subs = useQuery(queries.subs());   // the group's name only; read once
  const node = nodes.data?.find((item) => item.id === nodeId);
  const group = node ? groupOf(node) : undefined;
  const groupName = group === undefined ? null : group === SERVERS ? "Servers" : subs.data?.find((sub) => sub.id === group)?.name;
  const dialogs = useNodeDialogs();
  return (
    <div className="flex flex-col gap-3">
      <Link to="/nodes" search={listSearch(search, group)} className="inline-flex min-h-11 items-center gap-1.5 self-start text-sm font-semibold text-t2 hover:text-t1">
        <ArrowLeft size={16} aria-hidden />
        Servers{groupName ? ` · ${groupName}` : ""}
      </Link>
      <NodeDetailBody nodeId={nodeId} showName menu={dialogs.menu} />
      <NodeDialogs dialog={dialogs.dialog} onClose={dialogs.close} />
    </div>
  );
}

/** The $nodeId child of the /nodes layout: a sheet over the still-mounted list from 768 px, a page below it. */
export function NodeDetail() {
  const { nodeId } = useParams({ from: "/nodes/$nodeId" });
  const search = useSearch({ from: "/nodes/$nodeId" });
  const desktop = useMediaQuery(DESKTOP_QUERY);
  const id = /^\d+$/.test(nodeId) ? Number(nodeId) : Number.NaN;
  return desktop ? <DetailSheet nodeId={id} search={search} /> : <DetailPage nodeId={id} search={search} />;
}
