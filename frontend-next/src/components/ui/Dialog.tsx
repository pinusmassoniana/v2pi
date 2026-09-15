import { X } from "lucide-react";
import { Dialog as Primitive } from "radix-ui";
import { useCallback, useRef, useState, type ComponentProps, type ReactNode } from "react";
import { cn } from "../../lib/cn";
import { closeGuarded } from "../confirm";
import { Button } from "./Button";

/**
 * A dialog root. With `dirty` (and a controlled `open`), closing it by Escape, a click outside or a
 * close button asks before discarding the edits.
 */
export function Dialog({ dirty = false, onOpenChange, ...props }: ComponentProps<typeof Primitive.Root> & { dirty?: boolean }) {
  return (
    <Primitive.Root
      {...props}
      onOpenChange={(open) => (open ? onOpenChange?.(true) : void closeGuarded(dirty, () => onOpenChange?.(false)))}
    />
  );
}
export const DialogTrigger = Primitive.Trigger;
export const DialogClose = Primitive.Close;

/**
 * Escape closes only the overlay that holds focus. With one overlay opened over another (an export dialog over a
 * node's sheet), radix leaves the lower one listening for a moment after the upper one appears, and an Escape pressed
 * then closed the lower one while focus was already in the upper. Spread the result onto a radix Content.
 */
export function useEscapeWithin() {
  const ref = useRef<HTMLDivElement>(null);
  const onEscapeKeyDown = useCallback((event: KeyboardEvent) => {
    const target = event.target;
    if (ref.current && target instanceof Node && target !== document.body && !ref.current.contains(target)) event.preventDefault();
  }, []);
  return { ref, onEscapeKeyDown };
}

/** Where an overlay puts focus when it opens: its first field, or the overlay itself (one to read, not fill in). */
export type InitialFocus = "field" | "overlay";

const FIELD = "input:not([type=hidden]):not(:disabled), select:not(:disabled), textarea:not(:disabled)";

/**
 * Focus for an overlay opened by state or by a route rather than a Radix trigger. On open: its first field, else the
 * overlay itself — never the × that heads it, which stays in the tab order. On close: whatever held focus the moment
 * it mounted (the button, menu trigger or row link that opened it), since Radix has no trigger to return it to.
 * Spread the result onto a radix Content.
 */
export function useOverlayFocus(initial: InitialFocus = "field") {
  const [opener] = useState(() => (document.activeElement instanceof HTMLElement ? document.activeElement : null));
  const onOpenAutoFocus = useCallback((event: Event) => {
    const overlay = event.currentTarget;
    if (!(overlay instanceof HTMLElement)) return;
    event.preventDefault();
    const field = initial === "field" ? overlay.querySelector<HTMLElement>(FIELD) : null;
    (field ?? overlay).focus();
  }, [initial]);
  const onCloseAutoFocus = useCallback((event: Event) => {
    if (!opener?.isConnected) return;
    event.preventDefault();
    opener.focus();
  }, [opener]);
  return { onOpenAutoFocus, onCloseAutoFocus };
}

/** The × in an overlay's header: closes it like Escape does, asking first when it holds unsaved edits. */
export function CloseButton() {
  return (
    <Primitive.Close asChild>
      <Button size="icon" variant="ghost" aria-label="Close" className="-mr-1.5 -mt-1.5 size-8 shrink-0">
        <X size={16} aria-hidden />
      </Button>
    </Primitive.Close>
  );
}

/** A portal with the dimmed backdrop every dialog, sheet and confirmation sits on. */
export function OverlayPortal({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <Primitive.Portal>
      <Primitive.Overlay className={cn("fixed inset-0 z-50 bg-[rgba(4,3,10,.6)] backdrop-blur-sm", className)} />
      {children}
    </Primitive.Portal>
  );
}

export function DialogContent({ title, description, initialFocus, className, children }: {
  title: string;
  description?: string;
  initialFocus?: InitialFocus;
  className?: string;
  children: ReactNode;
}) {
  const escape = useEscapeWithin();
  const focus = useOverlayFocus(initialFocus);
  return (
    <OverlayPortal>
      <Primitive.Content
        {...escape}
        {...focus}
        // without a description, opt out explicitly instead of pointing at an element that is not there
        {...(description ? {} : { "aria-describedby": undefined })}
        className={cn(
          "glass fixed left-1/2 top-1/2 z-50 max-h-[90dvh] w-[min(34rem,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2 overflow-y-auto p-5",
          className,
        )}
      >
        <div className="flex items-start justify-between gap-3">
          <Primitive.Title className="text-base font-bold text-t1">{title}</Primitive.Title>
          <CloseButton />
        </div>
        {description ? <Primitive.Description className="mt-1 text-sm text-t2">{description}</Primitive.Description> : null}
        <div className="mt-4">{children}</div>
      </Primitive.Content>
    </OverlayPortal>
  );
}
