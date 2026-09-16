import { Dialog as Primitive } from "radix-ui";
import type { ReactNode } from "react";
import { cn } from "../../lib/cn";
import { CloseButton, Dialog, OverlayPortal, useEscapeWithin, useOverlayFocus, type InitialFocus } from "./Dialog";

/** A sheet root; `dirty` guards closing exactly as on Dialog. */
export const Sheet = Dialog;

/**
 * Bottom sheet on a phone; a right-hand panel over the page from 768 px (node detail, forms). Opened by
 * navigating to a route or by state rather than a `Dialog.Trigger`, so focus is placed and given back by
 * useOverlayFocus: the first field on open, and on close whatever held focus the moment this mounted (typically
 * the row or card link that opened it, which the list underneath keeps mounted for exactly this).
 */
export function SheetContent({ title, initialFocus, className, children, onEscapeKeyDown, onPointerDownOutside }: {
  title: string;
  initialFocus?: InitialFocus;
  className?: string;
  children: ReactNode;
  /** An extra guard beyond the escape-stacking fix below — e.g. refuse to close while a one-time secret is on display. */
  onEscapeKeyDown?: (event: KeyboardEvent) => void;
  onPointerDownOutside?: (event: Event) => void;
}) {
  const escape = useEscapeWithin();
  const focus = useOverlayFocus(initialFocus);
  return (
    <OverlayPortal className="bg-[rgba(4,3,10,.5)]">
      <Primitive.Content
        {...escape}
        {...focus}
        onEscapeKeyDown={(event) => { escape.onEscapeKeyDown(event); onEscapeKeyDown?.(event); }}
        onPointerDownOutside={onPointerDownOutside}
        aria-describedby={undefined}
        className={cn(
          "glass fixed inset-x-0 bottom-0 z-50 max-h-[88dvh] overflow-y-auto rounded-b-none p-5 " +
            "md:inset-y-0 md:left-auto md:right-0 md:max-h-none md:w-[min(32rem,100vw)] md:rounded-r-none",
          className,
        )}
      >
        <div className="flex items-start justify-between gap-3">
          <Primitive.Title className="min-w-0 truncate text-base font-bold text-t1">{title}</Primitive.Title>
          <CloseButton />
        </div>
        <div className="mt-4">{children}</div>
      </Primitive.Content>
    </OverlayPortal>
  );
}
