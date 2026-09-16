import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { keys } from "./keys";
import { useRefetchOnChange } from "./useRefetchOnChange";

describe("useRefetchOnChange", () => {
  it("the first known signature sets the baseline; a change refetches every key; the same one again does nothing", () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries").mockResolvedValue();
    const wrapper = ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client }, children);
    const { rerender } = renderHook(({ signature }: { signature: string | null }) => useRefetchOnChange(signature, [keys.rw, keys.network]), {
      wrapper, initialProps: { signature: null as string | null },
    });
    rerender({ signature: "1|true" });
    rerender({ signature: "1|true" });
    expect(invalidate).not.toHaveBeenCalled();
    rerender({ signature: null });   // unknown for a moment: neither a change nor a new baseline
    rerender({ signature: "1|false" });
    expect(invalidate.mock.calls.map(([filters]) => filters)).toEqual([{ queryKey: keys.rw }, { queryKey: keys.network }]);
    rerender({ signature: "1|false" });
    expect(invalidate).toHaveBeenCalledTimes(2);
  });
});
