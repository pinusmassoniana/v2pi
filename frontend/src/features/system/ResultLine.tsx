import { cn } from "../../lib/cn";

/**
 * What the last attempt in this editor said. A polite live region MOUNTED EMPTY, before anything has run: screen
 * readers announce reliably only a change inside a region that already existed, so the element has to be there
 * (taking no space) from the first render. One per editor, in one place, for the answer to land in.
 */
export function ResultLine({ result, className }: { result: { ok: boolean; text: string } | null; className?: string }) {
  if (!result) return <p role="status" className="sr-only" />;
  return (
    <p role="status" className={cn("min-w-0 truncate font-mono text-xs", result.ok ? "text-ok" : "text-bad", className)}>
      {result.ok ? "✓" : "✕"} {result.text}
    </p>
  );
}
