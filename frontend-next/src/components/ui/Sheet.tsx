import { Dialog as Primitive } from "radix-ui";
import type { ReactNode } from "react";
import { cn } from "../../lib/cn";

export const Sheet = Primitive.Root;

/** Bottom sheet on a phone; a right-hand panel over the page from 768 px (node detail, forms). */
export function SheetContent({ title, className, children }: { title: string; className?: string; children: ReactNode }) {
  return (
    <Primitive.Portal>
      <Primitive.Overlay className="fixed inset-0 z-50 bg-[rgba(4,3,10,.5)] backdrop-blur-sm" />
      <Primitive.Content
        aria-describedby={undefined}
        className={cn(
          "glass fixed inset-x-0 bottom-0 z-50 max-h-[88dvh] overflow-y-auto rounded-b-none p-5 " +
            "md:inset-y-0 md:left-auto md:right-0 md:max-h-none md:w-[min(32rem,100vw)] md:rounded-r-none",
          className,
        )}
      >
        <Primitive.Title className="text-base font-bold text-t1">{title}</Primitive.Title>
        <div className="mt-4">{children}</div>
      </Primitive.Content>
    </Primitive.Portal>
  );
}
