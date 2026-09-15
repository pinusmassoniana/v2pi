import { Toaster as Sonner, toast } from "sonner";
import { errText } from "../../api/client";

// Status messages expire: 8 s for success, 20 s for warnings and errors so they can actually be read.
export const TOAST_MS = { ok: 8_000, warn: 20_000, error: 20_000 } as const;

export function Toaster() {
  return <Sonner position="top-center" toastOptions={{ classNames: { toast: "glass text-t1", description: "text-t2" } }} />;
}

export function notifyOk(message: string): void {
  toast.success(message, { duration: TOAST_MS.ok });
}

/** Something that is not a failure but needs a look (an answer that did not arrive yet): amber, 20 s. */
export function notifyWarn(message: string): void {
  toast.warning(message, { duration: TOAST_MS.warn });
}

/** A failure. `sticky` keeps it until dismissed — for a state the operator has to act on, never for a routine refusal. */
export function notifyError(error: unknown, fallback: string, options: { sticky?: boolean } = {}): void {
  const message = errText(error, fallback);
  if (options.sticky) toast.error(message, { duration: Infinity, closeButton: true });
  else toast.error(message, { duration: TOAST_MS.error });
}
