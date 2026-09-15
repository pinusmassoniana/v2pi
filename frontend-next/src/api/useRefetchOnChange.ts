import { useQueryClient, type QueryKey } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

/**
 * Refetch `queryKeys` as soon as `signature` changes — for state the gateway changes on its own, with no write here to
 * invalidate anything (an auto-failover, the watchdog). `null` means not known yet; the first known signature only sets
 * the baseline.
 */
export function useRefetchOnChange(signature: string | null, queryKeys: readonly QueryKey[]): void {
  const queryClient = useQueryClient();
  const seen = useRef<string | null>(null);

  useEffect(() => {
    if (signature === null) return;
    const previous = seen.current;
    seen.current = signature;
    if (previous === null || previous === signature) return;
    for (const queryKey of queryKeys) void queryClient.invalidateQueries({ queryKey });
  }, [queryClient, signature, queryKeys]);
}
