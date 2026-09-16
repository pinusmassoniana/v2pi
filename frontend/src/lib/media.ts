import { useCallback, useSyncExternalStore } from "react";

/** The layout switch: from 768 px the desktop layout (sidebar, tables, sheets); below it the phone layout. */
export const DESKTOP_QUERY = "(min-width: 768px)";

function supported(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function";
}

/** Whether `query` matches now, re-rendering when it starts or stops matching. False where matchMedia is missing. */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback((onChange: () => void) => {
    if (!supported()) return () => {};
    const list = window.matchMedia(query);
    list.addEventListener("change", onChange);
    return () => list.removeEventListener("change", onChange);
  }, [query]);
  return useSyncExternalStore(subscribe, () => supported() && window.matchMedia(query).matches);
}
