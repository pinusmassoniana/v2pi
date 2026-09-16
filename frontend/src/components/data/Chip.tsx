import type { ComponentProps } from "react";
import { cn } from "../../lib/cn";
import type { Tone } from "./types";

const DOT: Record<Tone, string> = {
  ok: "before:bg-ok before:shadow-[0_0_8px_var(--ok)] [&_b]:text-ok",
  warn: "before:bg-warn before:shadow-[0_0_8px_var(--warn)] [&_b]:text-warn",
  bad: "before:bg-bad before:shadow-[0_0_8px_var(--bad)] [&_b]:text-bad",
  neutral: "before:bg-t3",
};

export const TINT: Record<Tone, string> = {
  ok: "bg-ok/12 text-ok",
  warn: "bg-warn/12 text-warn",
  bad: "bg-bad/12 text-bad",
  neutral: "",
};

export interface ChipProps extends ComponentProps<"span"> {
  tone?: Tone;
  /** No dot: the whole chip is tinted instead (e.g. "stale config"). */
  plain?: boolean;
}

/** A small status token: a dot in the state colour and a label whose <b> value takes that colour. */
export function Chip({ tone = "neutral", plain = false, className, children, ...props }: ChipProps) {
  return (
    <span
      data-tone={tone}
      className={cn(
        "inline-flex items-center gap-1.5 whitespace-nowrap rounded-full bg-glass-2 px-2.5 py-0.5 text-[11px] text-t2",
        "[&_b]:text-[10.5px] [&_b]:font-bold [&_b]:tracking-wide [&_b]:text-t1",
        plain ? TINT[tone] : cn("before:size-1.5 before:shrink-0 before:rounded-full before:content-['']", DOT[tone]),
        className,
      )}
      {...props}
    >
      {children}
    </span>
  );
}
