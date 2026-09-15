import type { ReactNode } from "react";
import { Button } from "../../components/ui/Button";
import { cn } from "../../lib/cn";
import { changesLabel } from "./rules";

export const GATEWAY_CHANGED = "The ruleset changed on the gateway since you started editing — Discard to load it";

export interface StagedBannerProps {
  changes: number;
  gatewayChanged: boolean;
  onDiscard: () => void;
  /** Apply staged, once saving is wired. */
  children?: ReactNode;
  className?: string;
}

/** R5: what is staged and not live yet, with Discard; polite, so a changed count is read without taking focus. */
export function StagedBanner({ changes, gatewayChanged, onDiscard, children, className }: StagedBannerProps) {
  return (
    <section aria-label="Staged changes" className={cn("glass flex flex-wrap items-center gap-2.5 border-warn/40 bg-solid p-3", className)}>
      <span aria-hidden className="grid size-6 shrink-0 place-items-center rounded-full bg-warn/15 text-xs font-bold text-warn">!</span>
      <div className="min-w-0 flex-1">
        <p role="status" className="text-sm text-t1">
          <b className="rounded-md bg-warn/15 px-1.5 py-0.5 text-[10.5px] font-bold tracking-wide text-warn">STAGED</b>
          {` · ${changesLabel(changes)} — not yet applied to the live config.`}
        </p>
        {gatewayChanged ? <p className="mt-1 text-xs text-warn">{GATEWAY_CHANGED}</p> : null}
      </div>
      <div className="flex items-center gap-2">
        <Button size="sm" onClick={onDiscard}>Discard</Button>
        {children}
      </div>
    </section>
  );
}
