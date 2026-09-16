import { useId, useState, type ReactNode } from "react";
import { cn } from "../../lib/cn";

export interface Step {
  /** Stable React key: a recommendation's title, say. */
  key: string;
  title: ReactNode;
  detail?: ReactNode;
}

function StepBody({ step, index, collapsible }: { step: Step; index: number; collapsible: boolean }) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const number = (
    <span aria-hidden className="grid size-5 shrink-0 place-items-center rounded-md bg-glass-2 font-mono text-[11px] font-semibold text-t2">{index + 1}</span>
  );
  if (collapsible) {
    return (
      <li className="flex min-w-0 flex-col gap-1">
        <button
          type="button"
          aria-expanded={open}
          aria-controls={id}
          onClick={() => setOpen((value) => !value)}
          className="flex min-h-9 w-full items-center gap-2.5 text-left text-[13px] font-semibold text-t1 focus-visible:outline-2 focus-visible:outline-g2"
        >
          {number}
          <span className="min-w-0 flex-1">{step.title}</span>
        </button>
        <div id={id} hidden={!open} className="pb-1 pl-7.5 text-xs leading-relaxed text-t2">{step.detail}</div>
      </li>
    );
  }
  return (
    <li className="flex min-w-0 gap-2.5">
      {number}
      <div className="min-w-0">
        <p className="text-[13px] font-semibold text-t1">{step.title}</p>
        {step.detail ? <div className="mt-0.5 text-xs leading-relaxed text-t2">{step.detail}</div> : null}
      </div>
    </li>
  );
}

/**
 * Numbered steps for the operator to do by hand — never checkboxes: nothing here is verified. An ordered list, so each
 * step is read with its number. `collapsible` shows titles only, each a button that opens its detail (a phone).
 */
export function StepList({ steps, collapsible = false, className }: { steps: readonly Step[]; collapsible?: boolean; className?: string }) {
  return (
    <ol className={cn("flex flex-col gap-2.5", className)}>
      {steps.map((step, index) => <StepBody key={step.key} step={step} index={index} collapsible={collapsible} />)}
    </ol>
  );
}
