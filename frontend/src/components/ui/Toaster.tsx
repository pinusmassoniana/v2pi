import { Toaster as Sonner, toast } from "sonner";
import { errText } from "../../api/client";

// Status messages expire: 8 s for success, 20 s for warnings and errors so they can actually be read.
export const TOAST_MS = { ok: 8_000, warn: 20_000, error: 20_000 } as const;

/**
 * Colour means state: a warning has to be amber, or "no answer yet" reads exactly like a failure. It is the one type
 * that gets a colour of its own — success and error keep the shared surface. The `!` is needed: sonner injects its own
 * unlayered stylesheet, whose `[data-sonner-toast][data-styled]` rule outranks any layered utility.
 */
const WARN_TOAST = "border-warn/40! text-warn!";

/**
 * The shared surface. `!` for the same reason WARN_TOAST needs it: sonner's unlayered
 * `[data-sonner-toast][data-styled]` rule sets its own background, colour and border, and outranks any
 * layered utility — without the modifier the glass surface and the text colour are simply not applied.
 */
const TOAST = "glass! text-t1!";

export function Toaster() {
  return <Sonner position="top-center" toastOptions={{ classNames: { toast: TOAST, description: "text-t2", warning: WARN_TOAST } }} />;
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
