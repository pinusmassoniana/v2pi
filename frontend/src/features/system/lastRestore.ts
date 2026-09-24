import { useSyncExternalStore } from "react";
import type { RestoreResult } from "../../api/client";

export interface LastRestore {
  result: RestoreResult;
  filename: string;
  /** Local epoch ms of when the reply arrived — the browser's clock, because nothing in the reply carries one. */
  at: number;
}

/**
 * The outcome of the restore made in THIS tab. It has to outlive the screen — the operator navigates to Home to
 * reconnect and comes back — but it must not outlive the tab: the reply names an absolute path on the gateway's
 * filesystem, and no System state is ever written to localStorage or sessionStorage. A module-level store, the same
 * shape `components/confirm.ts` uses, does exactly that and dies with the page.
 */
let current: LastRestore | null = null;
// Bumped when the session ends: a restore started before a logout can still answer after it, and that answer
// belongs to the session that asked, not to whoever logs in next.
let session = 0;
const listeners = new Set<() => void>();
const emit = () => { for (const listener of listeners) listener(); };

export function recordLastRestore(entry: LastRestore): void {
  current = entry;
  emit();
}

/** The session a restore or undo starts in; record its reply only while `lastRestoreSession()` still says so. */
export function lastRestoreSession(): number {
  return session;
}

/** Cleared with the query cache when the session ends, so a later login never sees the previous operator's restore. */
export function clearLastRestore(): void {
  session += 1;
  current = null;
  emit();
}

export function useLastRestore(): LastRestore | null {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    () => current,
  );
}
