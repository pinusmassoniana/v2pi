import { RefreshCw, X } from "lucide-react";
import { Button } from "../../components/ui/Button";
import { cn } from "../../lib/cn";
import type { Outcome } from "./subForm";

/** U3: the last Refresh all's outcome, kept on screen until dismissed. */
export function RefreshAllPanel({ outcome, onDismiss }: { outcome: Outcome; onDismiss: () => void }) {
  return (
    <div
      role="status"
      aria-label="Refresh all result"
      className={cn("glass flex items-start gap-2.5 p-3 text-sm text-t1", outcome.error ? "border-warn/50" : "border-ok/40")}
    >
      <RefreshCw size={15} aria-hidden className={cn("mt-0.5 shrink-0", outcome.error ? "text-warn" : "text-ok")} />
      <p className="min-w-0 flex-1 break-words">{outcome.text}</p>
      <Button size="icon" variant="ghost" className="size-7" aria-label="Dismiss refresh result" onClick={onDismiss}>
        <X size={14} aria-hidden />
      </Button>
    </div>
  );
}
