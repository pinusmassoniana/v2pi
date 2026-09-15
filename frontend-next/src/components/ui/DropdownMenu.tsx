import { DropdownMenu as Primitive } from "radix-ui";
import type { ComponentProps, ReactNode } from "react";
import { cn } from "../../lib/cn";

export const DropdownMenu = Primitive.Root;
export const DropdownMenuTrigger = Primitive.Trigger;

/** The menu panel: keyboard navigation, focus return and dismissal come from radix. */
export function DropdownMenuContent({ className, children, ...props }: ComponentProps<typeof Primitive.Content>) {
  return (
    <Primitive.Portal>
      <Primitive.Content
        align="end"
        sideOffset={6}
        {...props}
        className={cn("glass z-50 min-w-44 bg-solid p-1 text-sm text-t1", className)}
      >
        {children}
      </Primitive.Content>
    </Primitive.Portal>
  );
}

/** One action; a disabled item says why underneath, so the reason is read with it. */
export function DropdownMenuItem({ className, hint, children, ...props }: ComponentProps<typeof Primitive.Item> & { hint?: ReactNode }) {
  return (
    <Primitive.Item
      {...props}
      className={cn(
        "flex min-h-9 cursor-pointer select-none flex-col justify-center rounded-lg px-2.5 py-1.5 outline-none data-[highlighted]:bg-glass-2 " +
          "data-[disabled]:cursor-not-allowed data-[disabled]:opacity-60",
        className,
      )}
    >
      <span className="flex items-center gap-2">{children}</span>
      {hint ? <span className="text-[11px] text-t3">{hint}</span> : null}
    </Primitive.Item>
  );
}

export function DropdownMenuSeparator() {
  return <Primitive.Separator className="my-1 h-px bg-line" />;
}
