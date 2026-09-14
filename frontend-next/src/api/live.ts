import { useQuery, type QueryKey, type UseQueryOptions } from "@tanstack/react-query";

// TanStack Query runs one refetchInterval timer per observer and re-arms every observer's timer
// whenever the query updates, so a second polling component silently imposes its own cadence on
// the key (the fastest one wins). So exactly one component per key — the one showing it live —
// polls, through this hook, and sets the cadence; every other consumer calls useQuery(options) and
// reads the cache. The owner's interval is also how long the key stays fresh (pollingStaleTime in
// queryClient.ts), so mounting a reader does not fetch. ESLint forbids refetchInterval elsewhere.
export function usePolledQuery<TData, TKey extends QueryKey>(
  options: UseQueryOptions<TData, Error, TData, TKey>,
  intervalMs: number,
) {
  return useQuery({ ...options, refetchInterval: intervalMs, refetchIntervalInBackground: false });
}
