import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import type { Status } from "../../api/client";
import { keys } from "../../api/keys";

/**
 * The gateway can switch node on its own (auto-failover), with no write here to invalidate anything. When the
 * active node or the last failover changes, node health and the network read (events, the path, failovers) are
 * out of date: refetch them now instead of waiting out their polls. The first status seen only sets the baseline.
 */
export function useRefreshOnSwitch(status: Status | undefined): void {
  const queryClient = useQueryClient();
  const known = status !== undefined;
  const activeId = status?.active_node_id ?? null;
  const failoverAt = status?.last_failover_at ?? null;
  const seen = useRef<string | null>(null);

  useEffect(() => {
    if (!known) return;
    const current = `${activeId}|${failoverAt}`;
    const previous = seen.current;
    seen.current = current;
    if (previous === null || previous === current) return;
    void queryClient.invalidateQueries({ queryKey: keys.nodeHealth });
    void queryClient.invalidateQueries({ queryKey: keys.network });
  }, [queryClient, known, activeId, failoverAt]);
}
