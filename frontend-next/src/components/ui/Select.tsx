import type { ComponentProps } from "react";
import { cn } from "../../lib/cn";

/** A native select styled like Input. Spread react-hook-form's field into it: React 19 passes `ref` through as a prop. */
export function Select({ className, ...props }: ComponentProps<"select">) {
  return (
    <select
      className={cn(
        "h-10 rounded-xl border border-line bg-glass-2 px-3 text-sm text-t1 " +
          "focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-g2 disabled:opacity-60",
        className,
      )}
      {...props}
    />
  );
}
