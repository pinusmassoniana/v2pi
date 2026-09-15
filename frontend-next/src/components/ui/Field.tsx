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

export interface SegmentedFieldProps {
  legend: string;
  options: readonly string[];
  /** The radios' shared props — register(name) for react-hook-form. */
  radio: ComponentProps<"input">;
}

/** A choice of a few options as a segmented control: real radios, so arrow keys and screen readers work. */
export function SegmentedField({ legend, options, radio }: SegmentedFieldProps) {
  return (
    <fieldset className="flex min-w-0 flex-col gap-1">
      <legend className="mb-1 text-xs font-semibold text-t2">{legend}</legend>
      <div className="inline-flex self-start rounded-xl border border-line bg-glass p-0.5">
        {options.map((option) => (
          <label
            key={option}
            className={cn(
              "cursor-pointer rounded-[10px] px-3.5 py-1.5 text-[13px] font-semibold text-t3 transition-colors duration-150",
              "has-[:checked]:bg-glass-2 has-[:checked]:text-t1 has-[:checked]:shadow-[inset_0_0_0_1px_var(--line)]",
              "has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-g2",
            )}
          >
            <input type="radio" value={option} {...radio} className="sr-only" />
            {option}
          </label>
        ))}
      </div>
    </fieldset>
  );
}
