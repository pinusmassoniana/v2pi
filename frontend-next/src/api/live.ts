import { useQuery, type QueryKey, type UseQueryOptions } from "@tanstack/react-query";

// TanStack Query runs one refetchInterval timer per observer; only concurrent fetches are
// de-duplicated. A key polled from three mounted components is fetched on three clocks. So exactly
// one component per key — the one showing it live — polls, through this hook, and every other
// consumer calls useQuery(options) and reads the cache. ESLint forbids refetchInterval elsewhere.
export function usePolledQuery<TData, TKey extends QueryKey>(
  options: UseQueryOptions<TData, Error, TData, TKey>,
  intervalMs: number,
) {
  return useQuery({ ...options, refetchInterval: intervalMs, refetchIntervalInBackground: false });
}
