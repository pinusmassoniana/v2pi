// Promise-based, styled confirm — a themed replacement for window.confirm().
// confirmDialog(msg) resolves true/false when the user answers (or false on cancel/Escape).
interface ConfirmReq {
  message: string;
  resolve: (v: boolean) => void;
}

let _req = $state<ConfirmReq | null>(null);

export const confirmState = {
  get req() {
    return _req;
  },
};

export function confirmDialog(message: string): Promise<boolean> {
  // If a dialog is already open, settle it (false) before replacing it — otherwise the earlier
  // promise's resolve is dropped and its awaiter hangs forever (double-click / racing async flows).
  _req?.resolve(false);
  return new Promise<boolean>((resolve) => {
    _req = { message, resolve };
  });
}

export function resolveConfirm(value: boolean): void {
  _req?.resolve(value);
  _req = null;
}
