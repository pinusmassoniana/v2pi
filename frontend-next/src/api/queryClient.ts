import { QueryClient, type Query } from "@tanstack/react-query";
import { ApiError } from "./client";

/**
 * How long a cached key counts as fresh: its polling owner's interval while one is mounted, since
 * the owner refetches it at least that often — so a reader mounted between two polls shows the
 * cache instead of fetching again. A key nobody polls goes stale at once, and the next screen that
 * mounts it refetches.
 */
export function pollingStaleTime(query: Query): number {
  let fresh = 0;
  for (const observer of query.observers) {
    const interval = observer.options.refetchInterval;
    if (typeof interval === "number" && interval > 0 && (fresh === 0 || interval < fresh)) fresh = interval;
  }
  return fresh;
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // A 401 is a lost session, not a flaky request: the auth gate handles it. Anything else
        // gets one retry before the screen shows its error state.
        retry: (failureCount, error) =>
          !(error instanceof ApiError && error.status === 401) && failureCount < 1,
        staleTime: pollingStaleTime,
        refetchOnWindowFocus: true,
        refetchIntervalInBackground: false,
      },
      mutations: { retry: false },
    },
  });
}
