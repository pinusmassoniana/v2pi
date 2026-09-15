import { useQuery } from "@tanstack/react-query";
import { ApiError } from "../../api/client";
import { useConnectionBusy } from "../../api/invalidation";
import { queries } from "../../api/keys";

/**
 * The status poll failed, and not because the session is gone. Not while a connection write runs: every such write
 * holds the store's one lock for its whole transaction (a network apply for minutes), so a status poll can time out
 * with the panel alive and busy. Once the write settles, a failing poll counts again.
 */
export function useStatusUnreachable(): boolean {
  const status = useQuery(queries.status());   // the shell polls; fresh for its interval, so this does not fetch
  const busy = useConnectionBusy();
  return status.isError && !(status.error instanceof ApiError && status.error.status === 401) && !busy;
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
