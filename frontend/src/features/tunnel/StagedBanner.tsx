import type { ReactNode } from "react";
import { Button } from "../../components/ui/Button";
import { cn } from "../../lib/cn";
import { changesLabel } from "./rules";

export const GATEWAY_CHANGED = "The ruleset changed on the gateway since you started editing — Discard to load it";

export interface StagedBannerProps {
  /** Something is staged: show the banner. While nothing is, only its empty live region stays mounted. */
  staged: boolean;
  changes: number;
  gatewayChanged: boolean;
  onDiscard: () => void;
  /** Apply staged, once saving is wired. */
  children?: ReactNode;
  className?: string;
}

/**
 * R5: what is staged and not live yet, with Discard. The STAGED sentence and the gateway-changed notice are one polite
 * live region, so a changed count or notice is read without taking focus. It stays mounted — empty, taking no space —
 * while nothing is staged: screen readers announce reliably only a change inside a live region that already exists,
 * so a banner that mounted already filled would often go unread.
 */
export function StagedBanner({ staged, changes, gatewayChanged, onDiscard, children, className }: StagedBannerProps) {
  return (
    <section
      aria-label={staged ? "Staged changes" : undefined}
      className={staged ? cn("glass flex flex-wrap items-center gap-2.5 border-warn/40 bg-solid p-3", className) : "contents"}
    >
      {staged ? <span aria-hidden className="grid size-6 shrink-0 place-items-center rounded-full bg-warn/15 text-xs font-bold text-warn">!</span> : null}
      <div role="status" className={staged ? "min-w-0 flex-1 basis-56" : "sr-only"}>
        {staged ? (
          <>
            <p className="text-sm text-t1">
              <b className="rounded-md bg-warn/15 px-1.5 py-0.5 text-[10.5px] font-bold tracking-wide text-warn">STAGED</b>
              {` · ${changesLabel(changes)} — not yet applied to the live config.`}
            </p>
            {gatewayChanged ? <p className="mt-1 text-xs text-warn">{GATEWAY_CHANGED}</p> : null}
          </>
        ) : null}
      </div>
      {staged ? (
        <div className="flex items-center gap-2">
          <Button size="sm" onClick={onDiscard}>Discard</Button>
          {children}
        </div>
      ) : null}
    </section>
  );
}
