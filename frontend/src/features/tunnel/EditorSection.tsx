import { ChevronDown } from "lucide-react";
import { useId, type ReactNode } from "react";
import { cn } from "../../lib/cn";

export interface EditorSectionProps {
  title: string;
  /** Short context beside the title ("xhttp nodes only"). */
  note?: string;
  /** Phone: the header is a button that opens and closes the section. */
  collapsible: boolean;
  open: boolean;
  onToggle: () => void;
  /** What a closed section's header says about its state. */
  summary?: string;
  /** A control beside the header — a section's own switch — kept outside the header button, never nested in it. */
  aside?: ReactNode;
  children: ReactNode;
}

/** One editor section: a fieldset named by its legend, whose header sticks while its fields scroll. */
export function EditorSection({ title, note, collapsible, open, onToggle, summary, aside, children }: EditorSectionProps) {
  const id = useId();
  const shown = !collapsible || open;
  const heading = (
    <>
      <span className="text-[10.5px] font-semibold uppercase tracking-[.07em] text-t2">{title}</span>
      {note ? <span className="truncate text-[11px] font-normal normal-case text-t3">{note}</span> : null}
    </>
  );
  return (
    <fieldset className="m-0 min-w-0 border-0 border-t border-line p-0 first:border-t-0">
      <legend className="float-left flex w-full items-center gap-2 py-2.5 min-[1180px]:sticky min-[1180px]:top-0 min-[1180px]:z-10 min-[1180px]:bg-solid">
        {collapsible ? (
          <button
            type="button"
            aria-expanded={open}
            aria-controls={id}
            onClick={onToggle}
            className="flex min-h-9 min-w-0 flex-1 items-center gap-2 text-left focus-visible:outline-2 focus-visible:outline-g2"
          >
            {heading}
            {!open && summary ? <span className="ml-auto shrink-0 text-[11px] text-t3">{summary}</span> : null}
            <ChevronDown size={15} aria-hidden className={cn("shrink-0 text-t3 transition-transform duration-150", !open && summary ? "" : "ml-auto", open && "rotate-180")} />
          </button>
        ) : (
          <span className="flex min-w-0 flex-1 items-baseline gap-2">{heading}</span>
        )}
        {aside}
      </legend>
      <div id={id} hidden={!shown} className="clear-both flex flex-col gap-2.5 pb-3">
        {children}
      </div>
    </fieldset>
  );
}
