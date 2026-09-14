import type { QueryClient, QueryKey } from "@tanstack/react-query";
import { keys } from "./keys";

const NODE_LIST = [keys.nodes, keys.nodeHealth, keys.profiles, keys.subs];
const CONNECTION = [keys.status, keys.nodes, keys.nodeHealth, keys.network];
const SUBSCRIPTION = [keys.subs, keys.nodes, keys.nodeHealth];
// refreshSub / refreshAllSubs can restart the active tunnel (subs/service.py:_restart_active ->
// apply_node -> apply_net) and give new nodes the subscription's default profile on merge
// (subs/reconcile.py: reconcile), so a refresh reaches further than a plain edit of the
// subscription row does.
const SUBSCRIPTION_REFRESH = [...SUBSCRIPTION, keys.status, keys.network, keys.profiles];
const PROFILE = [keys.profiles, keys.nodes, keys.status];

/**
 * What each write changes, named after its api method. "all" invalidates every query: a restore
 * replaces the whole configuration. Preview / validate / preset / short-id reads are absent on
 * purpose — invalidating after them would overwrite the form their reply was staged into.
 *
 * A few rows reach further than their own resource because the backend re-applies the live
 * tunnel as a side effect: applyProfileActive, putRouting, putSettings and resetSettings can all
 * call reapply_active_node -> apply_node -> apply_net (routes.py), so each also invalidates
 * keys.network on top of its own resource.
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
  setDefaultProfile: PROFILE, applyProfileActive: [...PROFILE, keys.network],
  putRouting: [keys.routing, keys.status, keys.network],
  putNetwork: [keys.network, keys.status],
  putRw: [keys.rw], addRwClient: [keys.rw], setRwClientEnabled: [keys.rw], deleteRwClient: [keys.rw],
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
