import { useQueryClient, type QueryClient, type QueryKey } from "@tanstack/react-query";
import { useCallback } from "react";
import { api } from "./client";
import { keys } from "./keys";

const NODE_LIST = [keys.nodes, keys.nodeHealth, keys.profiles, keys.subs];
const CONNECTION = [keys.status, keys.nodes, keys.nodeHealth, keys.network];
const SUBSCRIPTION = [keys.subs, keys.nodes, keys.nodeHealth];
// refreshSub / refreshAllSubs can restart the active tunnel (subs/service.py:_restart_active ->
// apply_node -> apply_net) and give new nodes the subscription's default profile on merge
// (subs/reconcile.py: reconcile), so a refresh reaches further than a plain edit of the
// subscription row does.
const SUBSCRIPTION_REFRESH = [...SUBSCRIPTION, keys.status, keys.network, keys.profiles];
// Every profile write can end up re-applying the active node when it turns out to use the
// profile being changed (add can't; the other four can, conditionally — routes.py:
// set_default_profile:899, update_profile:918, delete_profile:954, apply_profile_active:938
// unconditionally). One group, one cheap extra GET on addProfile, no per-row special case.
const PROFILE = [keys.profiles, keys.nodes, keys.status, keys.network];
// Every remote-access write reaches reapply_active_node, either through _rw_apply (grants) or
// _rw_revoke -> _rw_revoke_apply (revocations) — routes.py:1783,2618.
const RW = [keys.rw, keys.status, keys.network];

/**
 * What each write changes, named after its api method. "all" invalidates every query: a restore
 * replaces the whole configuration. Preview / validate / preset / short-id reads are absent on
 * purpose — invalidating after them would overwrite the form their reply was staged into.
 *
 * Several groups reach further than their own resource because the backend re-applies the live
 * tunnel as a side effect (reapply_active_node -> apply_node -> apply_net, routes.py): see the
 * PROFILE, RW and SUBSCRIPTION_REFRESH comments above, plus putRouting/putSettings/resetSettings
 * below, which reapply unconditionally or conditionally on their own.
 */
export const INVALIDATES = {
  addNode: NODE_LIST, updateNode: NODE_LIST, deleteNode: NODE_LIST,
  importNodes: NODE_LIST, detachNodes: NODE_LIST, reorderNodes: NODE_LIST,
  apply: CONNECTION, disconnect: CONNECTION, connectBest: CONNECTION,
  rollback: CONNECTION, xrayStart: CONNECTION, xrayStop: CONNECTION,
  probeTcp: [keys.nodeHealth], probeHttp: [keys.nodeHealth], probeNode: [keys.nodeHealth],
  addSub: SUBSCRIPTION, updateSub: SUBSCRIPTION, deleteSub: SUBSCRIPTION,
  refreshSub: SUBSCRIPTION_REFRESH, refreshAllSubs: SUBSCRIPTION_REFRESH,
  addProfile: PROFILE, updateProfile: PROFILE, deleteProfile: PROFILE,
  setDefaultProfile: PROFILE, applyProfileActive: PROFILE,
  putRouting: [keys.routing, keys.status, keys.network],
  putNetwork: [keys.network, keys.status],
  putRw: RW, addRwClient: RW, setRwClientEnabled: RW, deleteRwClient: RW,
  putSettings: [keys.settings, keys.status, keys.network],
  resetSettings: [keys.settings, keys.status, keys.network],
  createToken: [keys.tokens], deleteToken: [keys.tokens],
  changePassword: [],
  restore: "all",
} satisfies Record<string, readonly QueryKey[] | "all">;

export type MutationName = keyof typeof INVALIDATES;

export async function invalidate(client: QueryClient, name: MutationName): Promise<void> {
  const targets: readonly QueryKey[] | "all" = INVALIDATES[name];
  if (targets === "all") {
    await client.invalidateQueries();
    return;
  }
  await Promise.all(targets.map((queryKey) => client.invalidateQueries({ queryKey })));
}

type Api = typeof api;

/**
 * The api write `name`, bound to its invalidation: call it like `api[name]`; once it succeeds, the
 * queries it changes are invalidated. Refetching is started, not awaited, so a success message
 * does not wait for the screens to reload.
 */
export function useApiWrite<N extends MutationName>(name: N) {
  const queryClient = useQueryClient();
  return useCallback(
    async (...args: Parameters<Api[N]>): Promise<Awaited<ReturnType<Api[N]>>> => {
      const write = api[name] as (...params: Parameters<Api[N]>) => ReturnType<Api[N]>;
      const result = await write(...args);
      void invalidate(queryClient, name);
      return result;
    },
    [queryClient, name],
  );
}
