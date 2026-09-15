import { useQuery } from "@tanstack/react-query";
import type { Status } from "../../api/client";
import { queries } from "../../api/keys";

/** The part of the 3 s status poll the Nodes screens show. Not `server_now`, which changes on every poll. */
export interface NodesStatus {
  activeId: number | null;
  /** When the active node was connected (epoch s). */
  activeSince: number | null;
  lastFailoverAt: number | null;
  failoverEnabled: boolean;
  /** xray-core runs (connectedState). */
  running: boolean;
}

function pickNodesStatus(status: Status): NodesStatus {
  return {
    activeId: status.active_node_id,
    activeSince: status.active_since,
    lastFailoverAt: status.last_failover_at,
    failoverEnabled: status.failover_enabled === true,
    running: status.running,
  };
}

/**
 * The status cache (the shell polls it) through a module-level select: the picked object keeps its identity while
 * its fields are unchanged, so a poll re-renders a reader only when one of them, or the poll's failing, changes.
 */
export function useNodesStatus(): { status: NodesStatus | undefined; statusError: boolean } {
  const { data, isError } = useQuery({ ...queries.status(), select: pickNodesStatus });
  return { status: data, statusError: isError };
}
