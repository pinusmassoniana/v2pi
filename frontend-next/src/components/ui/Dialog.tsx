import { Dialog as Primitive } from "radix-ui";
import type { ComponentProps, ReactNode } from "react";
import { cn } from "../../lib/cn";
import { closeGuarded } from "../confirm";

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

/** A portal with the dimmed backdrop every dialog, sheet and confirmation sits on. */
export function OverlayPortal({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <Primitive.Portal>
      <Primitive.Overlay className={cn("fixed inset-0 z-50 bg-[rgba(4,3,10,.6)] backdrop-blur-sm", className)} />
      {children}
    </Primitive.Portal>
  );
}

export function DialogContent({ title, description, className, children }: {
  title: string;
  description?: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <OverlayPortal>
      <Primitive.Content
        // without a description, opt out explicitly instead of pointing at an element that is not there
        {...(description ? {} : { "aria-describedby": undefined })}
        className={cn(
          "glass fixed left-1/2 top-1/2 z-50 max-h-[90dvh] w-[min(34rem,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2 overflow-y-auto p-5",
          className,
        )}
      >
        <Primitive.Title className="text-base font-bold text-t1">{title}</Primitive.Title>
        {description ? <Primitive.Description className="mt-1 text-sm text-t2">{description}</Primitive.Description> : null}
        <div className="mt-4">{children}</div>
      </Primitive.Content>
    </OverlayPortal>
  );
}
