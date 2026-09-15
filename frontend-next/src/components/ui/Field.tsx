import { useId, type ComponentProps, type ReactNode } from "react";
import { cn } from "../../lib/cn";
import { Input } from "./Input";

export interface FieldShellProps {
  id: string;
  label: string;
  required?: boolean;
  /** Short help beside the label (a limit, "optional"); not part of the control's name. */
  hint?: ReactNode;
  error?: string;
  className?: string;
  children: ReactNode;
}

/** A label, its hint and its error around one control; the error is tied to the control by aria-describedby. */
export function FieldShell({ id, label, required = false, hint, error, className, children }: FieldShellProps) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-1", className)}>
      <div className="flex items-baseline justify-between gap-2">
        {/* The required mark is drawn by CSS, so it stays out of the label's text and the control's name. */}
        <label htmlFor={id} className={cn("text-xs font-semibold text-t2", required && "after:ml-1 after:text-t3 after:content-['*']")}>
          {label}
        </label>
        {hint ? <span className="text-[11px] text-t3">{hint}</span> : null}
      </div>
      {children}
      {error ? <p id={`${id}-error`} className="text-[11px] text-bad">{error}</p> : null}
    </div>
  );
}

export type TextFieldProps = ComponentProps<"input"> & {
  label: string;
  hint?: ReactNode;
  error?: string;
  /** Classes of the wrapper; `className` styles the input. */
  fieldClassName?: string;
};

/** A labelled input. Spread react-hook-form's register() into it: React 19 passes `ref` through as a prop. */
export function TextField({ label, hint, error, required, fieldClassName, className, id, ...input }: TextFieldProps) {
  const generated = useId();
  const inputId = id ?? generated;
  return (
    <FieldShell id={inputId} label={label} required={required} hint={hint} error={error} className={fieldClassName}>
      <Input
        id={inputId}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? `${inputId}-error` : undefined}
        className={cn(error && "border-bad/60", className)}
        {...input}
      />
    </FieldShell>
  );
}

export interface SegmentedOption {
  value: string;
  /** What the option reads as; its value when absent. */
  label?: string;
  /** Classes for this option — `has-[:checked]:text-bad` to colour it while checked, say. */
  className?: string;
}

export interface SegmentedFieldProps {
  legend: string;
  options: readonly (string | SegmentedOption)[];
  /** Uncontrolled, for react-hook-form: the radios' shared props — register(name). */
  radio?: ComponentProps<"input">;
  /** Controlled instead: the checked value, and what picking an option does. */
  value?: string;
  onValueChange?: (value: string) => void;
  /** The radio group's name when controlled; a generated one when absent. */
  name?: string;
  disabled?: boolean;
  className?: string;
}

/**
 * A choice of a few options as a segmented control: real radios in a fieldset named by its legend, so arrow keys and
 * screen readers work. Register it with react-hook-form (`radio`) or control it (`value` + `onValueChange`). A row of
 * options wider than its container scrolls sideways instead of wrapping.
 */
export function SegmentedField({ legend, options, radio, value, onValueChange, name, disabled, className }: SegmentedFieldProps) {
  const generated = useId();
  const group = name ?? generated;
  return (
    <fieldset disabled={disabled} className={cn("flex min-w-0 flex-col gap-1", className)}>
      <legend className="mb-1 text-xs font-semibold text-t2">{legend}</legend>
      <div className="inline-flex max-w-full self-start overflow-x-auto rounded-xl border border-line bg-glass p-0.5">
        {options.map((entry) => {
          const option: SegmentedOption = typeof entry === "string" ? { value: entry } : entry;
          const input: ComponentProps<"input"> = onValueChange
            ? { name: group, checked: value === option.value, onChange: () => onValueChange(option.value) }
            : (radio ?? {});
          return (
            <label
              key={option.value}
              className={cn(
                "shrink-0 cursor-pointer whitespace-nowrap rounded-[10px] px-3.5 py-1.5 text-[13px] font-semibold text-t3 transition-colors duration-150",
                "has-[:checked]:bg-glass-2 has-[:checked]:text-t1 has-[:checked]:shadow-[inset_0_0_0_1px_var(--line)]",
                "has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-g2 has-[:disabled]:cursor-not-allowed has-[:disabled]:opacity-60",
                option.className,
              )}
            >
              <input type="radio" value={option.value} {...input} className="sr-only" />
              {option.label ?? option.value}
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}
