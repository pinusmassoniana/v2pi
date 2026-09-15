import { useIsMutating, useQueryClient, type QueryClient, type QueryKey } from "@tanstack/react-query";
import { useCallback } from "react";
import { ApiError, api } from "./client";
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
 * The mutation key shared by every write that changes what carries the tunnel — connect / reload config (apply),
 * connect best, roll back, disconnect, xray-core start / stop — so each of their controls stays disabled while any
 * of them runs, and two never overlap.
 */
export const CONNECTION_WRITE = ["connection"] as const;

/** TCP or HTTP ping of a group (Servers) or of every node (⌘K): one sweep at a time; the variables say which kind. */
export const PING_KEY = ["nodes", "ping"] as const;

/** Test all (real) over a group's rows: one run at a time, however many screens show the button. */
export const TEST_ALL_KEY = ["nodes", "testAll"] as const;

/** A bulk assign, detach or delete over the selected nodes: one at a time. */
export const BULK_KEY = ["nodes", "bulk"] as const;

/** Refresh of one subscription or of all of them (Subscriptions, ⌘K): they block each other. */
export const SUBS_REFRESH_KEY = ["subs", "refresh"] as const;

/** A gateway settings write: one at a time, so a rollback or a re-read only ever reasons about one. */
export const SETTINGS_WRITE = ["settings-write"] as const;

/**
 * Routing Save. It always re-applies the tunnel (put_routing -> reapply_active_node), so it is a connection write: its
 * key starts with CONNECTION_WRITE, and every filter on that key — useConnectionBusy, isConnectionBusy — counts it.
 */
export const ROUTING_WRITE = [...CONNECTION_WRITE, "routing"] as const;

/** A tuning-profile write that leaves the live tunnel alone: create, or update / delete of a profile the tunnel is not on. */
export const PROFILE_WRITE = ["profiles", "write"] as const;

/**
 * A tuning-profile write that moves the live tunnel: apply to the active node, make default, or update / delete of the
 * profile the tunnel runs on (`is_active`). A connection write too — its key starts with CONNECTION_WRITE.
 */
export const PROFILE_CONNECTION_WRITE = [...CONNECTION_WRITE, "profile"] as const;

/** A connection write (CONNECTION_WRITE) is running. */
export function useConnectionBusy(): boolean {
  return useIsMutating({ mutationKey: CONNECTION_WRITE }) > 0;
}

/** Any tuning-profile write (PROFILE_WRITE or PROFILE_CONNECTION_WRITE) is running: the Anti-DPI screen's one busy flag. */
export function useProfileBusy(): boolean {
  const plain = useIsMutating({ mutationKey: PROFILE_WRITE });
  const live = useIsMutating({ mutationKey: PROFILE_CONNECTION_WRITE });
  return plain + live > 0;
}

/** Said when another connection write started while a confirmation was open, so nothing was sent. */
export const CONNECTION_BUSY = "Another connection change is still running — try again when it finishes";

/**
 * A 502 on a connection write: the write and its re-apply run in one store transaction (spec §13.2), so a failed
 * apply rolls the write back too — the gateway still holds what it had before the write was sent. Shared by Routing
 * (`useRoutingActions.ts`), the Anti-DPI profile editor (`ProfileEditor.tsx`) and the profile row actions
 * (`useProfileActions.ts`), so each shows this instead of the raw apply error; `outcome` names what did not happen
 * ("not applied", "default not changed", "not deleted").
 */
export function saveRefusedMessage(error: ApiError, outcome = "not saved"): string {
  return `${outcome} — applying to the tunnel failed: ${error.message}`;
}

/**
 * A connection write is running right now. For code that awaited something first (a confirmation): the busy flag
 * it rendered with may be out of date, so it checks again before starting its own write.
 */
export function isConnectionBusy(client: QueryClient): boolean {
  return client.isMutating({ mutationKey: CONNECTION_WRITE }) > 0;
}

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
