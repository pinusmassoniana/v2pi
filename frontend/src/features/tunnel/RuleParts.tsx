import type { ReactNode } from "react";
import { cn } from "../../lib/cn";
import type { RuleAction } from "./rules";

/** R1 action colours: direct the accent, proxy the download series, block bad. */
export const ACTION_TEXT: Readonly<Record<RuleAction, string>> = { direct: "text-g1", proxy: "text-g2", block: "text-bad" };

const ACTION_PILL: Readonly<Record<RuleAction, string>> = { direct: "bg-g1/12 text-g1", proxy: "bg-g2/18 text-g2", block: "bg-bad/12 text-bad" };

/** A segmented action option takes its colour while checked. */
export const ACTION_CHECKED: Readonly<Record<RuleAction, string>> = {
  direct: "has-[:checked]:text-g1", proxy: "has-[:checked]:text-g2", block: "has-[:checked]:text-bad",
};

export function ActionPill({ action, className }: { action: RuleAction; className?: string }) {
  return (
    <span data-action={action} className={cn("inline-flex min-w-14 justify-center rounded-md px-2 py-0.5 text-[11px] font-bold", ACTION_PILL[action], className)}>
      {action}
    </span>
  );
}

export function TypeChip({ children }: { children: ReactNode }) {
  return <span className="inline-flex rounded-md border border-line bg-glass-2 px-1.5 py-0.5 font-mono text-[11px] text-t2">{children}</span>;
}
