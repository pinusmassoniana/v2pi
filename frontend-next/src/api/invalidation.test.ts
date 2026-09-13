import { QueryClient, type QueryKey } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import clientSource from "./client.ts?raw";
import { INVALIDATES, invalidate, type MutationName } from "./invalidation";
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
    // One-line methods whose body calls mutate(…); putSettings is the one multi-line write.
    // logout is excluded on purpose: the auth gate clears the whole cache instead.
    const oneLine = [...clientSource.matchAll(/^ {2}(?:async )?(\w+)\([^\n]*\bmutate\(/gm)].map((m) => m[1]);
    const writes = [...new Set([...oneLine, "putSettings"])].sort();
    expect(writes).toEqual(Object.keys(INVALIDATES).sort());
  });

  it("matches the spec's table for the rows later screens depend on", () => {
    expect(INVALIDATES.apply).toEqual([keys.status, keys.nodes, keys.nodeHealth, keys.network]);
    expect(INVALIDATES.refreshSub).toEqual([keys.subs, keys.nodes, keys.nodeHealth]);
    expect(INVALIDATES.probeNode).toEqual([keys.nodeHealth]);
    expect(INVALIDATES.changePassword).toEqual([]);
    expect(INVALIDATES.restore).toBe("all");
  });
});
