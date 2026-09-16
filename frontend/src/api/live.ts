import { useQuery, type QueryKey, type UseQueryOptions } from "@tanstack/react-query";

// In TanStack Query 5.102.8 every observer that sets refetchInterval arms a timer of its own, and every
// update of the query re-arms all of them. A second polling component therefore adds no requests, but it
// silently takes over the key's cadence: the fastest interval wins. So exactly one component per key — the
// one showing it live — polls, through this hook, and every other consumer calls useQuery(options) and reads
// the cache. The owner's interval is also how long the key stays fresh (pollingStaleTime in queryClient.ts),
// so mounting a reader does not fetch. ESLint forbids refetchInterval anywhere else.
export function usePolledQuery<TData, TKey extends QueryKey>(
  options: UseQueryOptions<TData, Error, TData, TKey>,
  intervalMs: number,
) {
  return useQuery({ ...options, refetchInterval: intervalMs, refetchIntervalInBackground: false });
}
