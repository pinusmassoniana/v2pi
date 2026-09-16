import type { ReactNode } from "react";
import { ErrorState, Skeleton } from "../ui/States";

/** The parts of a query result a card needs to decide what to show. */
export interface CardQuery {
  data: unknown;
  isError: boolean;
  refetch: () => Promise<unknown>;
}

function retryFailed(queries: readonly CardQuery[]): void {
  for (const query of queries) if (query.isError) void query.refetch();
}

/**
 * What a card shows instead of its content, or null when every query has data: an error with Retry when a
 * read failed with nothing to show, otherwise a skeleton (never the empty message while still loading).
 */
export function cardFallback(queries: readonly CardQuery[], message: string, skeletonClassName = "h-28"): ReactNode {
  if (queries.every((query) => query.data !== undefined)) return null;
  if (queries.some((query) => query.isError && query.data === undefined)) {
    return <ErrorState message={message} onRetry={() => retryFailed(queries)} />;
  }
  return <Skeleton className={skeletonClassName} />;
}

/** A read failed but the card still has its last good data: say so above it, politely, with Retry. */
export function staleNotice(queries: readonly CardQuery[], message: string): ReactNode {
  if (!queries.some((query) => query.isError && query.data !== undefined)) return null;
  return <ErrorState role="status" message={`${message} — showing the last data`} onRetry={() => retryFailed(queries)} />;
}
