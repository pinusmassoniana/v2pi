import { cn } from "../../lib/cn";
import { FORM_CHANGED, isStaleRun, type CheckResult } from "../../lib/staleResult";

/**
 * A Validate result: "✓ …" in the ok colour or "✗ …" in the bad one, or — once the input it checked is no longer the
 * form's — the form-changed note instead. A polite live region, so the answer is read: it is mounted empty (and
 * takes no space) until something ran, because screen readers announce reliably only a change inside a live region
 * that already exists. Keep it mounted in one place for the answer to land in.
 */
export function CheckLine({ result, liveKey, className }: { result: CheckResult | null; liveKey: string; className?: string }) {
  if (!result) return <p role="status" className="sr-only" />;
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
