import { partialMatchKey, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef, useSyncExternalStore } from "react";
import { ApiError } from "../../api/client";
import { CONNECTION_WRITE, useConnectionBusy } from "../../api/invalidation";
import { queries } from "../../api/keys";

/**
 * ms timestamp of the last time a connection write (CONNECTION_WRITE) left "pending" on this client: settled
 * successfully or failed. A status poll can time out while such a write holds the store's lock; that failure is
 * stale the instant the write lets go, so `useStatusUnreachable` counts only a failure newer than this — the next
 * poll after the write settles, never the one that failed under its lock (spec §5.1).
 */
function useConnectionWriteSettledAt(): number {
  const client = useQueryClient();
  const settledAtRef = useRef(0);
  return useSyncExternalStore(
    useCallback(
      (onStoreChange) => client.getMutationCache().subscribe((event) => {
        if (event.type !== "updated" || (event.action.type !== "success" && event.action.type !== "error")) return;
        const key = event.mutation.options.mutationKey;
        if (key === undefined || !partialMatchKey(key, CONNECTION_WRITE)) return;
        settledAtRef.current = Date.now();
        onStoreChange();
      }),
      [client],
    ),
    () => settledAtRef.current,
  );
}

/**
 * The status poll failed, and not because the session is gone, and not with a failure left over from before a
 * connection write's lock let go. Not while a connection write runs: every such write holds the store's one lock for
 * its whole transaction (a network apply for minutes), so a status poll can time out with the panel alive and busy.
 * After the write settles, only its next failed poll counts — the poll that failed under the lock does not.
 */
export function useStatusUnreachable(): boolean {
  const status = useQuery(queries.status());   // the shell polls; fresh for its interval, so this does not fetch
  const busy = useConnectionBusy();
  const settledAt = useConnectionWriteSettledAt();
  return status.isError && status.errorUpdatedAt > settledAt
    && !(status.error instanceof ApiError && status.error.status === 401) && !busy;
}

/** Every screen keeps its last data on a failed read, so a dead panel would look alive. Say it once. */
export function OfflineBanner() {
  const status = useQuery(queries.status());
  const unreachable = useStatusUnreachable();
  if (!unreachable) return null;
  const since = status.dataUpdatedAt
    ? new Date(status.dataUpdatedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : null;
  return (
    <div role="status" aria-live="polite" className="flex items-center gap-2 border-b border-bad/40 bg-bad/15 px-4 py-2 text-xs font-medium text-t1 md:px-5">
      <span aria-hidden className="size-2 rounded-full bg-bad" />
      Can't reach the panel — {since ? `showing data from ${since}` : "no data loaded yet"}. Retrying…
    </div>
  );
}
