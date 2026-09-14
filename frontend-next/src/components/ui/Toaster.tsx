import { Toaster as Sonner, toast } from "sonner";
import { errText } from "../../api/client";

// Status messages expire: 8 s for success, 20 s for errors so they can actually be read.
export const TOAST_MS = { ok: 8_000, error: 20_000 } as const;

export function Toaster() {
  return <Sonner position="top-center" toastOptions={{ classNames: { toast: "glass text-t1", description: "text-t2" } }} />;
}

export function notifyOk(message: string): void {
  toast.success(message, { duration: TOAST_MS.ok });
}

export function notifyError(error: unknown, fallback: string): void {
  toast.error(errText(error, fallback), { duration: TOAST_MS.error });
}
