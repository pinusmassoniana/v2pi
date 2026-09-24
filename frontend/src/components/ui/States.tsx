import type { ReactNode } from "react";
import { cn } from "../../lib/cn";
import { Button } from "./Button";

/** Loading placeholder. Never render the empty message while data is still loading. */
export function Skeleton({ className }: { className?: string }) {
  return <div aria-hidden className={cn("animate-pulse rounded-lg bg-glass-2", className)} />;
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div role="status" className="glass flex flex-col items-center gap-1 p-8 text-center">
      <p className="text-sm font-semibold text-t1">{title}</p>
      {children ? <div className="text-xs text-t2">{children}</div> : null}
    </div>
  );
}

/** A failure with an optional Retry. `role="status"` for a polite note (e.g. a refresh failed but data is shown). */
export function ErrorState({ message, onRetry, retryLabel = "Retry", role = "alert" }: {
  message: string; onRetry?: () => void; retryLabel?: string; role?: "alert" | "status";
}) {
  return (
    <div role={role} className="glass flex items-center justify-between gap-3 border-bad/40 p-4">
      <p className="text-sm text-bad">{message}</p>
      {onRetry ? <Button size="sm" onClick={onRetry}>{retryLabel}</Button> : null}
    </div>
  );
}

/** The parts of a query a control needs to say that its read failed. */
export interface ReadQuery {
  data: unknown;
  isError: boolean;
  refetch: () => Promise<unknown>;
}

/**
 * A read that a control depends on failed with nothing to show: said on one line where the control is, with Retry,
 * so the control is never just disabled or empty with no reason. Renders nothing while the read is fine.
 */
export function ReadError({ query, message, className }: { query: ReadQuery; message: string; className?: string }) {
  if (!query.isError || query.data !== undefined) return null;
  return (
    <p role="alert" className={cn("flex flex-wrap items-center gap-2 text-xs text-bad", className)}>
      {message}
      <Button size="sm" variant="ghost" onClick={() => void query.refetch()}>Retry</Button>
    </p>
  );
}
