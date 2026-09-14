import { useQuery } from "@tanstack/react-query";
import { ApiError } from "../../api/client";
import { queries } from "../../api/keys";

/** Every screen keeps its last data on a failed read, so a dead panel would look alive. Say it once. */
export function OfflineBanner() {
  const status = useQuery(queries.status());   // reads the cache; the shell owns polling
  const unreachable = status.isError && !(status.error instanceof ApiError && status.error.status === 401);
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
