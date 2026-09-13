import type { QueryClient, QueryKey } from "@tanstack/react-query";
import { keys } from "./keys";

const NODE_LIST = [keys.nodes, keys.nodeHealth, keys.profiles, keys.subs];
const CONNECTION = [keys.status, keys.nodes, keys.nodeHealth, keys.network];
const SUBSCRIPTION = [keys.subs, keys.nodes, keys.nodeHealth];
const PROFILE = [keys.profiles, keys.nodes, keys.status];

/**
 * What each write changes, named after its api method. "all" invalidates every query: a restore
 * replaces the whole configuration. Preview / validate / preset / short-id reads are absent on
 * purpose — invalidating after them would overwrite the form their reply was staged into.
 */
export const INVALIDATES = {
  addNode: NODE_LIST, updateNode: NODE_LIST, deleteNode: NODE_LIST,
  importNodes: NODE_LIST, detachNodes: NODE_LIST, reorderNodes: NODE_LIST,
  apply: CONNECTION, disconnect: CONNECTION, connectBest: CONNECTION,
  rollback: CONNECTION, xrayStart: CONNECTION, xrayStop: CONNECTION,
  probeTcp: [keys.nodeHealth], probeHttp: [keys.nodeHealth], probeNode: [keys.nodeHealth],
  addSub: SUBSCRIPTION, updateSub: SUBSCRIPTION, deleteSub: SUBSCRIPTION,
  refreshSub: SUBSCRIPTION, refreshAllSubs: SUBSCRIPTION,
  addProfile: PROFILE, updateProfile: PROFILE, deleteProfile: PROFILE,
  setDefaultProfile: PROFILE, applyProfileActive: PROFILE,
  putRouting: [keys.routing, keys.status],
  putNetwork: [keys.network, keys.status],
  putRw: [keys.rw], addRwClient: [keys.rw], setRwClientEnabled: [keys.rw], deleteRwClient: [keys.rw],
  putSettings: [keys.settings, keys.status], resetSettings: [keys.settings, keys.status],
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
