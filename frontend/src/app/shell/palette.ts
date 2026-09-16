import { useSyncExternalStore } from "react";

export type PaletteMode = "all" | "nodes";
interface PaletteState { open: boolean; mode: PaletteMode }

let state: PaletteState = { open: false, mode: "all" };
const listeners = new Set<() => void>();
const set = (next: PaletteState) => {
  state = next;
  for (const listener of listeners) listener();
};

/** Open ⌘K. "nodes" narrows it to the node list, where picking a node connects it. */
export function openPalette(mode: PaletteMode = "all"): void {
  set({ open: true, mode });
}

export function closePalette(): void {
  if (state.open) set({ ...state, open: false });
}

export function usePalette(): PaletteState {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    () => state,
  );
}
