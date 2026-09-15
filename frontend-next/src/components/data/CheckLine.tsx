import { cn } from "../../lib/cn";
import { FORM_CHANGED, isStaleRun, type CheckResult } from "../../lib/staleResult";

/**
 * A Validate result: "✓ …" in the ok colour or "✗ …" in the bad one, or — once the input it checked is no longer the
 * form's — the form-changed note instead. Nothing until something ran. A polite live region, so the answer is read.
 */
export function CheckLine({ result, liveKey, className }: { result: CheckResult | null; liveKey: string; className?: string }) {
  if (!result) return null;
  const stale = isStaleRun(result.key, liveKey);
  return (
    <p
      role="status"
      data-stale={stale || undefined}
      title={stale ? undefined : result.text}
      className={cn("min-w-0 truncate font-mono text-xs", stale ? "text-t3" : result.ok ? "text-ok" : "text-bad", className)}
    >
      {stale ? FORM_CHANGED : result.text}
    </p>
  );
}
