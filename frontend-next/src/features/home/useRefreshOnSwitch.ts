import type { Status } from "../../api/client";
import { keys } from "../../api/keys";
import { useRefetchOnChange } from "../../api/useRefetchOnChange";

const AFTER_SWITCH = [keys.nodeHealth, keys.network] as const;

/**
 * The gateway can switch node on its own (auto-failover), with no write here to invalidate anything. When the
 * active node or the last failover changes, node health and the network read (events, the path, failovers) are
 * out of date: refetch them now instead of waiting out their polls. The first status seen only sets the baseline.
 */
export function useRefreshOnSwitch(status: Status | undefined): void {
  useRefetchOnChange(status ? `${status.active_node_id}|${status.last_failover_at}` : null, AFTER_SWITCH);
}
