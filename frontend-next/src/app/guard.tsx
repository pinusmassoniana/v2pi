import { useBlocker } from "@tanstack/react-router";
import { useEffect } from "react";
import { confirm } from "../components/confirm";

export { closeGuarded } from "../components/confirm";

let dirtyCount = 0;

/** True while any mounted screen or sheet holds unsaved edits. */
export function hasUnsavedEdits(): boolean {
  return dirtyCount > 0;
}

/** Mark this screen or sheet as holding unsaved edits while `dirty`; the shell's blocker asks before leaving. */
export function useUnsavedGuard(dirty: boolean): void {
  useEffect(() => {
    if (!dirty) return;
    dirtyCount += 1;
    return () => { dirtyCount -= 1; };
  }, [dirty]);
}

/**
 * The one navigation blocker, mounted by the shell: blocks in-app navigation and tab close while
 * anything holds unsaved edits, asking once however many screens and sheets are dirty. (Each
 * blocker asks in turn, so one per guard would prompt once per guard.)
 */
export function useUnsavedEditsBlocker(): void {
  const { status, proceed, reset } = useBlocker({
    shouldBlockFn: hasUnsavedEdits,
    enableBeforeUnload: hasUnsavedEdits,
    withResolver: true,
  });

  useEffect(() => {
    if (status !== "blocked") return;
    let stale = false;
    void confirm("Discard unsaved changes and leave this screen?", { confirmLabel: "Discard" }).then((ok) => {
      if (stale) return;
      if (ok) proceed?.();
      else reset?.();
    });
    return () => { stale = true; };
  }, [status, proceed, reset]);
}
