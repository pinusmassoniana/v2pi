import type { ComponentProps } from "react";
import { cn } from "../../lib/cn";

export function GlassCard({ className, ...props }: ComponentProps<"section">) {
  return <section className={cn("glass p-4", className)} {...props} />;
}
