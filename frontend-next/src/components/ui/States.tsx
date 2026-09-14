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
