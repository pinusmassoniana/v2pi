import { useBlocker } from "@tanstack/react-router";
import { useEffect } from "react";
import { confirm } from "../components/confirm";

let dirtyCount = 0;

/** True while any mounted screen or sheet holds unsaved edits. */
export function hasUnsavedEdits(): boolean {
  return dirtyCount > 0;
}

/** Block in-app navigation and tab close while `dirty`, asking once before discarding. */
export function useUnsavedGuard(dirty: boolean): void {
  useEffect(() => {
    if (!dirty) return;
    dirtyCount += 1;
    return () => { dirtyCount -= 1; };
  }, [dirty]);

  const { status, proceed, reset } = useBlocker({
    shouldBlockFn: () => dirty,
    enableBeforeUnload: () => dirty,
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

/** Close a sheet or dialog, asking first when it holds unsaved edits. */
export async function closeGuarded(dirty: boolean, close: () => void): Promise<void> {
  if (!dirty || (await confirm("Discard unsaved changes?", { confirmLabel: "Discard" }))) close();
}
