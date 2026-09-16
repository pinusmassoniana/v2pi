import { DropdownMenu as Primitive } from "radix-ui";
import { useCallback, useRef, type ComponentProps, type ReactNode } from "react";
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

/**
 * For items that open a dialog or a confirmation: `after(action)` in an item's onSelect runs the action only once the
 * menu has closed and given focus back to its trigger — spread `onCloseAutoFocus` onto the DropdownMenuContent. A
 * dialog opened any earlier finds focus on nothing (the menu is already gone, the trigger not focused yet), so it
 * has nothing to give focus back to when it closes.
 */
export function useAfterMenu() {
  const pending = useRef<(() => void) | null>(null);
  const after = useCallback((action: () => void) => { pending.current = action; }, []);
  const onCloseAutoFocus = useCallback(() => {
    const action = pending.current;
    pending.current = null;
    // Radix focuses the trigger right after this handler returns; the action runs after that.
    if (action) window.setTimeout(action, 0);
  }, []);
  return { after, onCloseAutoFocus };
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
