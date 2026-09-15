import { Dialog as Primitive } from "radix-ui";
import { useState, type ReactNode } from "react";
import { cn } from "../../lib/cn";
import { CloseButton, Dialog, OverlayPortal, useEscapeWithin } from "./Dialog";

/** A sheet root; `dirty` guards closing exactly as on Dialog. */
export const Sheet = Dialog;

/**
 * Bottom sheet on a phone; a right-hand panel over the page from 768 px (node detail, forms). Opened by
 * navigating to a route rather than clicking a `Dialog.Trigger`, so Radix has no trigger to return focus to on
 * close (its default `onCloseAutoFocus` would otherwise send it nowhere) — restore it ourselves, to whatever
 * held focus the moment this mounted (typically the row or card link that opened it, which the list underneath
 * keeps mounted for exactly this).
 */
export function SheetContent({ title, className, children }: { title: string; className?: string; children: ReactNode }) {
  const [opener] = useState(() => (document.activeElement instanceof HTMLElement ? document.activeElement : null));
  const escape = useEscapeWithin();
  return (
    <OverlayPortal className="bg-[rgba(4,3,10,.5)]">
      <Primitive.Content
        {...escape}
        aria-describedby={undefined}
        onCloseAutoFocus={(event) => {
          if (opener?.isConnected) {
            event.preventDefault();
            opener.focus();
          }
        }}
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
