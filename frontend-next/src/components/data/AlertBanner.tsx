import type { ReactNode } from "react";
import { cn } from "../../lib/cn";
import { Button } from "../ui/Button";

const SURFACE = {
  bad: "border-bad/40 bg-linear-to-r from-bad/20 to-bad/5",
  warn: "border-warn/30 bg-linear-to-r from-warn/15 to-warn/5",
} as const;
const ICON = { bad: "bg-bad/20 text-bad", warn: "bg-warn/20 text-warn" } as const;

export interface AlertAction {
  label: string;
  /** Shown while `busy`. */
  busyLabel?: string;
  busy?: boolean;
  /** Unavailable for another reason than its own work (e.g. a related write is running). */
  disabled?: boolean;
  onClick: () => void;
}

export interface AlertBannerProps {
  tone: "warn" | "bad";
  /** Bold lead-in, and the banner's accessible name. */
  title: string;
  /** Continues the title's sentence. */
  text?: ReactNode;
  /** A decorative glyph. */
  icon?: string;
  /** Extra content under the sentence (e.g. chips). */
  children?: ReactNode;
  action?: AlertAction;
  /** Renders a dismiss button. Omit for conditions that must stay visible. */
  onDismiss?: () => void;
  className?: string;
}

/**
 * A problem worth interrupting for: a group named by its title. Only the title is a live region — "alert" when
 * bad, "status" when a warning — so a sentence that changes every second (a rate, an age) is not re-announced
 * each time: it sits outside the live region, marked aria-live="off".
 */
export function AlertBanner({ tone, title, text, icon = "!", children, action, onDismiss, className }: AlertBannerProps) {
  return (
    <div
      role="group"
      aria-label={title}
      data-tone={tone}
      className={cn("flex flex-wrap items-center gap-x-3 gap-y-2 rounded-2xl border px-3 py-2.5 text-[12.5px] text-t1 backdrop-blur-md", SURFACE[tone], className)}
    >
      <span aria-hidden className={cn("grid size-6 shrink-0 place-items-center rounded-lg text-[13px] font-extrabold", ICON[tone])}>{icon}</span>
      <div className="min-w-0 flex-1">
        <p>
          <b role={tone === "bad" ? "alert" : "status"} aria-label={title} className="font-bold">{title}</b>
          {text ? <>{" "}<span aria-live="off" className="text-t2">{text}</span></> : null}
        </p>
        {children}
      </div>
      {action ? (
        <Button size="sm" variant={tone === "bad" ? "danger" : "secondary"} disabled={action.busy || action.disabled} onClick={action.onClick}>
          {action.busy ? (action.busyLabel ?? action.label) : action.label}
        </Button>
      ) : null}
      {onDismiss ? (
        <Button variant="ghost" size="sm" className="size-7 px-0 text-base" aria-label={`Dismiss: ${title}`} onClick={onDismiss}>
          ×
        </Button>
      ) : null}
    </div>
  );
}
