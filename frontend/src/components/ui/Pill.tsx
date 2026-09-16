import type { ComponentProps } from "react";
import { cn } from "../../lib/cn";

export type PillTone = "ok" | "warn" | "bad" | "neutral";

const TONES: Record<PillTone, string> = {
  ok: "border-ok/30 bg-ok/10 text-ok",
  warn: "border-warn/30 bg-warn/10 text-warn",
  bad: "border-bad/30 bg-bad/10 text-bad",
  neutral: "border-line bg-glass-2 text-t2",
};

export interface PillProps extends ComponentProps<"span"> {
  tone?: PillTone;
  dot?: boolean;
}

export function Pill({ tone = "neutral", dot = false, className, children, ...props }: PillProps) {
  return (
    <span
      className={cn("inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 py-1 text-[11.5px] font-semibold", TONES[tone], className)}
      {...props}
    >
      {dot ? <span aria-hidden className="size-1.5 rounded-full bg-current shadow-[0_0_8px_currentColor]" /> : null}
      {children}
    </span>
  );
}
