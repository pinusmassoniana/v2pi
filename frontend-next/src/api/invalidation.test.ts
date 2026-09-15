import { QueryClient, QueryClientProvider, type QueryKey } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { api } from "./client";
import clientSource from "./client.ts?raw";
import {
  CONNECTION_WRITE, INVALIDATES, PROFILE_CONNECTION_WRITE, PROFILE_WRITE, ROUTING_WRITE, invalidate, isConnectionBusy, useApiWrite,
  useConnectionBusy, useProfileBusy, type MutationName,
} from "./invalidation";
import { keys } from "./keys";

describe("invalidation map", () => {
  it.each(Object.entries(INVALIDATES))("%s invalidates exactly what it changes", async (name, targets) => {
    const client = new QueryClient();
    const spy = vi.spyOn(client, "invalidateQueries").mockResolvedValue();
    await invalidate(client, name as MutationName);
    if (targets === "all") {
      expect(spy).toHaveBeenCalledTimes(1);
      expect(spy).toHaveBeenCalledWith();
    } else {
      expect(spy).toHaveBeenCalledTimes((targets as readonly QueryKey[]).length);
      for (const queryKey of targets as readonly QueryKey[]) expect(spy).toHaveBeenCalledWith({ queryKey });
    }
  });

  it("covers every write the API client can make, and nothing else", () => {
    // One-line methods whose body calls mutate(…) — every write is written that way.
    // logout is excluded on purpose: the auth gate clears the whole cache instead.
    const oneLine = [...clientSource.matchAll(/^ {2}(?:async )?(\w+)\([^\n]*\bmutate\(/gm)].map((m) => m[1]);
    const writes = [...new Set(oneLine)].sort();
    expect(writes).toEqual(Object.keys(INVALIDATES).sort());
  });

  it("matches the spec's table for the rows later screens depend on", () => {
    expect(INVALIDATES.apply).toEqual([keys.status, keys.nodes, keys.nodeHealth, keys.network]);
    expect(INVALIDATES.probeNode).toEqual([keys.nodeHealth]);
    expect(INVALIDATES.changePassword).toEqual([]);
    expect(INVALIDATES.restore).toBe("all");
  });

  it("also invalidates the tunnel-reapplying side effects a plain resource write does not have", () => {
    // refreshSub/refreshAllSubs can restart the active tunnel and give new nodes the
    // subscription's default profile (subs/service.py + subs/reconcile.py).
    expect(INVALIDATES.refreshSub).toEqual([keys.subs, keys.nodes, keys.nodeHealth, keys.status, keys.network, keys.profiles]);
    expect(INVALIDATES.refreshAllSubs).toEqual([keys.subs, keys.nodes, keys.nodeHealth, keys.status, keys.network, keys.profiles]);
    // putRouting, putSettings and resetSettings each re-apply the live tunnel server-side on
    // their own (routes.py: reapply_active_node -> apply_node -> apply_net).
    expect(INVALIDATES.putRouting).toEqual([keys.routing, keys.status, keys.network]);
    expect(INVALIDATES.putSettings).toEqual([keys.settings, keys.status, keys.network]);
    expect(INVALIDATES.resetSettings).toEqual([keys.settings, keys.status, keys.network]);
    // Every profile write can re-apply the active node when it turns out to use the profile
    // being changed (set_default_profile/update_profile/delete_profile conditionally,
    // apply_profile_active unconditionally) — one shared group, so addProfile (which never
    // re-applies) also invalidates network as a cheap accepted side effect.
    expect(INVALIDATES.addProfile).toEqual([keys.profiles, keys.nodes, keys.status, keys.network]);
    expect(INVALIDATES.updateProfile).toEqual([keys.profiles, keys.nodes, keys.status, keys.network]);
    expect(INVALIDATES.deleteProfile).toEqual([keys.profiles, keys.nodes, keys.status, keys.network]);
    expect(INVALIDATES.setDefaultProfile).toEqual([keys.profiles, keys.nodes, keys.status, keys.network]);
    expect(INVALIDATES.applyProfileActive).toEqual([keys.profiles, keys.nodes, keys.status, keys.network]);
    // Every remote-access write reaches reapply_active_node too, via _rw_apply (grants) or
    // _rw_revoke -> _rw_revoke_apply (revocations) — routes.py:1783,2618.
    expect(INVALIDATES.putRw).toEqual([keys.rw, keys.status, keys.network]);
    expect(INVALIDATES.addRwClient).toEqual([keys.rw, keys.status, keys.network]);
    expect(INVALIDATES.setRwClientEnabled).toEqual([keys.rw, keys.status, keys.network]);
    expect(INVALIDATES.deleteRwClient).toEqual([keys.rw, keys.status, keys.network]);
    // addSub/updateSub/deleteSub stay unchanged: routes.py shows none of them call
    // refresh/reapply.
    expect(INVALIDATES.addSub).toEqual([keys.subs, keys.nodes, keys.nodeHealth]);
    expect(INVALIDATES.updateSub).toEqual([keys.subs, keys.nodes, keys.nodeHealth]);
    expect(INVALIDATES.deleteSub).toEqual([keys.subs, keys.nodes, keys.nodeHealth]);
  });
});

describe("useApiWrite", () => {
  it("calls the api write with its arguments, returns its reply, and invalidates that write's queries without waiting for them", async () => {
    const client = new QueryClient();
    // refetches that never finish must not hold up the caller
    const invalidateQueries = vi.spyOn(client, "invalidateQueries").mockReturnValue(new Promise<void>(() => {}));
    const apply = vi.spyOn(api, "apply").mockResolvedValue({ ok: true });
    const wrapper = ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client }, children);
    const { result } = renderHook(() => useApiWrite("apply"), { wrapper });

    await expect(result.current(7)).resolves.toEqual({ ok: true });
    expect(apply).toHaveBeenCalledWith(7);
    expect(invalidateQueries.mock.calls.map(([filters]) => filters)).toEqual(INVALIDATES.apply.map((queryKey) => ({ queryKey })));
  });

  it("does not invalidate when the write fails", async () => {
    const client = new QueryClient();
    const invalidateQueries = vi.spyOn(client, "invalidateQueries");
    vi.spyOn(api, "rollback").mockRejectedValue(new Error("nope"));
    const wrapper = ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client }, children);
    const { result } = renderHook(() => useApiWrite("rollback"), { wrapper });

    await expect(result.current()).rejects.toThrow("nope");
    expect(invalidateQueries).not.toHaveBeenCalled();
  });
});

/** Start a mutation under `mutationKey` that runs until the returned function is called. */
function hold(client: QueryClient, mutationKey: QueryKey): () => Promise<void> {
  let release: () => void = () => {};
  const done = new Promise<void>((resolve) => { release = resolve; });
  const running = client.getMutationCache().build(client, { mutationKey, mutationFn: () => done }).execute(undefined);
  return () => act(async () => { release(); await running; });
}

describe("Tunnel mutation keys", () => {
  it("Routing Save and the profile writes that move the tunnel are connection writes; plain profile writes are not", async () => {
    const client = new QueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client }, children);
    const { result } = renderHook(() => ({ connection: useConnectionBusy(), profile: useProfileBusy() }), { wrapper });
    expect(ROUTING_WRITE.slice(0, CONNECTION_WRITE.length)).toEqual([...CONNECTION_WRITE]);
    expect(PROFILE_CONNECTION_WRITE.slice(0, CONNECTION_WRITE.length)).toEqual([...CONNECTION_WRITE]);

    let release = hold(client, ROUTING_WRITE);
    expect(isConnectionBusy(client)).toBe(true);
    await waitFor(() => expect(result.current).toEqual({ connection: true, profile: false }));
    await release();
    await waitFor(() => expect(result.current).toEqual({ connection: false, profile: false }));

    release = hold(client, PROFILE_CONNECTION_WRITE);
    await waitFor(() => expect(result.current).toEqual({ connection: true, profile: true }));
    await release();
    await waitFor(() => expect(result.current).toEqual({ connection: false, profile: false }));

    release = hold(client, PROFILE_WRITE);
    expect(isConnectionBusy(client)).toBe(false);
    await waitFor(() => expect(result.current).toEqual({ connection: false, profile: true }));
    await release();
    await waitFor(() => expect(result.current).toEqual({ connection: false, profile: false }));
  });
});
