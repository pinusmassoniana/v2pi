import { useSyncExternalStore } from "react";

export interface ConfirmRequest {
  message: string;
  confirmLabel: string;
  danger: boolean;
  resolve: (ok: boolean) => void;
}

let current: ConfirmRequest | null = null;
const listeners = new Set<() => void>();
const emit = () => { for (const listener of listeners) listener(); };

/** Ask before a risky action. Resolves false on Cancel, Escape or a click outside. */
export function confirm(message: string, options: { confirmLabel?: string; danger?: boolean } = {}): Promise<boolean> {
  // A newer request replaces an open one: settle the old promise so its awaiter never hangs.
  current?.resolve(false);
  return new Promise<boolean>((resolve) => {
    current = { message, confirmLabel: options.confirmLabel ?? "Confirm", danger: options.danger ?? true, resolve };
    emit();
  });
}

/** Close a sheet or dialog, asking first when it holds unsaved edits. */
export async function closeGuarded(dirty: boolean, close: () => void): Promise<void> {
  if (!dirty || (await confirm("Discard unsaved changes?", { confirmLabel: "Discard" }))) close();
}

export function settleConfirm(ok: boolean): void {
  const request = current;
  current = null;
  request?.resolve(ok);
  emit();
}

export function useConfirmRequest(): ConfirmRequest | null {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    () => current,
  );
}
