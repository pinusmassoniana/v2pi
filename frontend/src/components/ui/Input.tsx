import type { ComponentProps } from "react";
import { cn } from "../../lib/cn";

export function Input({ className, ...props }: ComponentProps<"input">) {
  return (
    <input
      className={cn(
        "h-10 w-full rounded-xl border border-line bg-glass-2 px-3 text-sm text-t1 placeholder:text-t3 " +
          "focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-g2 disabled:opacity-60",
        className,
      )}
      {...props}
    />
  );
}
