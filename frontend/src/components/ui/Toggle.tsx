import { Switch } from "radix-ui";

export interface ToggleProps {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  /** Accessible name — required, a switch has no visible text of its own. */
  label: string;
  disabled?: boolean;
}

export function Toggle({ checked, onCheckedChange, label, disabled }: ToggleProps) {
  return (
    <Switch.Root
      checked={checked}
      onCheckedChange={onCheckedChange}
      disabled={disabled}
      aria-label={label}
      className="relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border border-line bg-glass-2 transition-colors data-[state=checked]:border-transparent data-[state=checked]:bg-brand disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-g2"
    >
      <Switch.Thumb className="block size-4 translate-x-0.5 rounded-full bg-white shadow transition-transform data-[state=checked]:translate-x-[17px]" />
    </Switch.Root>
  );
}
