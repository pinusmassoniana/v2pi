import { cva, type VariantProps } from "class-variance-authority";
import { Slot } from "radix-ui";
import type { ComponentProps } from "react";
import { cn } from "../../lib/cn";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-xl font-semibold transition-colors " +
    "disabled:pointer-events-none disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-g2",
  {
    variants: {
      variant: {
        primary: "bg-brand text-[#07060d] shadow-[0_6px_20px_rgba(124,92,255,.35)]",
        secondary: "border border-line bg-glass-2 text-t1 hover:bg-glass",
        ghost: "text-t2 hover:bg-glass-2 hover:text-t1",
        danger: "border border-bad/40 bg-bad/15 text-bad hover:bg-bad/25",
      },
      size: {
        md: "h-9 px-3.5 text-[13px]",
        sm: "h-8 px-3 text-xs",
        icon: "size-9 text-[13px]",
      },
    },
    defaultVariants: { variant: "secondary", size: "md" },
  },
);

export interface ButtonProps extends ComponentProps<"button">, VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

export function Button({ className, variant, size, asChild = false, type, ...props }: ButtonProps) {
  const classes = cn(buttonVariants({ variant, size }), className);
  if (asChild) return <Slot.Root className={classes} {...props} />;
  return <button type={type ?? "button"} className={classes} {...props} />;
}
