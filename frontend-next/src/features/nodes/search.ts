// The Servers list's URL state (#/nodes?group=…&q=…&sort=…&dir=…) and the density preference.
import { z } from "zod";
import { SERVERS, SORT_KEYS, type GroupKey, type SortDir, type SortKey } from "./list";

// The router parses search values as JSON, so "?group=2" arrives as the number 2 and "?q=123" as 123. Anything that
// does not fit is dropped rather than failing the route; defaults are left out of the URL.
export const nodesSearchSchema = z.object({
  group: z.union([z.literal(SERVERS), z.number().int().positive(), z.string().regex(/^\d+$/).transform(Number)]).optional().catch(undefined),
  q: z.union([z.string(), z.number().transform(String)]).optional().catch(undefined),
  sort: z.enum(SORT_KEYS).optional().catch(undefined),
  dir: z.enum(["asc", "desc"]).optional().catch(undefined),
});

export type NodesSearch = z.output<typeof nodesSearchSchema>;

export interface ListState { group?: GroupKey; q: string; sort: SortKey; dir: SortDir }

/** The search params with their defaults filled in (the group stays optional: its default depends on the data). */
export function listState(search: NodesSearch): ListState {
  return { group: search.group, q: search.q ?? "", sort: search.sort ?? "pos", dir: search.dir ?? "asc" };
}

/** Search params for a list state, leaving out every default. */
export function toSearch(state: ListState): NodesSearch {
  return {
    group: state.group,
    q: state.q === "" ? undefined : state.q,
    sort: state.sort === "pos" ? undefined : state.sort,
    dir: state.dir === "asc" ? undefined : state.dir,
  };
}

/** N4: same key and values as the Svelte panel. */
export const DENSITY_KEY = "nodes-density";

export function readDense(): boolean {
  try {
    return localStorage.getItem(DENSITY_KEY) === "1";
  } catch {
    return false;
  }
}

export function writeDense(dense: boolean): void {
  try {
    localStorage.setItem(DENSITY_KEY, dense ? "1" : "0");
  } catch {
    // blocked site data: the density lasts until the page reloads
  }
}
