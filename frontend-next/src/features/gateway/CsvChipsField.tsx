import { X } from "lucide-react";
import { useId, useState, type KeyboardEvent, type ReactNode } from "react";
import { FieldShell } from "../../components/ui/Field";
import { cn } from "../../lib/cn";
import { parseCsv } from "./rwForm";

export interface CsvChipsFieldProps {
  label: string;
  hint?: ReactNode;
  placeholder?: string;
  values: readonly string[];
  /** What the gateway holds: a chip not among these is highlighted as not saved yet. */
  saved: readonly string[];
  onChange: (values: string[]) => void;
  /** Names a chip's remove button: "server name" → "Remove server name www.microsoft.com". */
  item: string;
  error?: string;
  /** An amber border for a warning that is not an error (the SNI check). */
  warn?: boolean;
  disabled?: boolean;
  /** Beside the input (Generate). */
  action?: ReactNode;
}

/**
 * A csv edited as chips: typing a comma or Enter, or leaving the field, turns what was typed into chips; × removes one.
 * The field's value is the list; the backend receives it joined by commas.
 */
export function CsvChipsField({ label, hint, placeholder, values, saved, onChange, item, error, warn, disabled, action }: CsvChipsFieldProps) {
  const id = useId();
  const [draft, setDraft] = useState("");
  const savedSet = new Set(saved.map((value) => value.toLowerCase()));

  function commit(text: string) {
    const added = parseCsv(text).filter((value) => !values.includes(value));
    setDraft("");
    if (added.length > 0) onChange([...values, ...new Set(added)]);
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter") {
      event.preventDefault();
      commit(draft);
    } else if (event.key === "Backspace" && draft === "" && values.length > 0) {
      onChange(values.slice(0, -1));
    }
  }

  return (
    <FieldShell id={id} label={label} hint={hint} error={error}>
      <div className="flex items-start gap-2">
        <div
          className={cn(
            "flex min-h-10 min-w-0 flex-1 flex-wrap items-center gap-1.5 rounded-xl border border-line bg-glass-2 px-2 py-1.5 has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-g2",
            warn && "border-warn/70",
            error && "border-bad/60",
            disabled && "opacity-60",
          )}
        >
          <ul aria-label={`${label} list`} className="contents">
            {values.map((value) => (
              <li
                key={value}
                data-unsaved={savedSet.has(value.toLowerCase()) ? undefined : "true"}
                className={cn(
                  "inline-flex max-w-full items-center gap-1 rounded-lg border border-line bg-glass px-2 py-0.5 font-mono text-xs text-t1",
                  !savedSet.has(value.toLowerCase()) && "border-g2/60 bg-g2/15",
                )}
              >
                <span className="truncate">{value}</span>
                <button
                  type="button"
                  aria-label={`Remove ${item} ${value}`}
                  disabled={disabled}
                  onClick={() => onChange(values.filter((other) => other !== value))}
                  className="grid size-4 place-items-center rounded text-t3 hover:text-t1 focus-visible:outline-2 focus-visible:outline-g2"
                >
                  <X size={12} aria-hidden />
                </button>
              </li>
            ))}
          </ul>
          <input
            id={id}
            value={draft}
            placeholder={values.length === 0 ? placeholder : undefined}
            disabled={disabled}
            autoComplete="off"
            spellCheck={false}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? `${id}-error` : undefined}
            onChange={(event) => {
              const text = event.target.value;
              if (text.includes(",")) commit(text);
              else setDraft(text);
            }}
            onKeyDown={onKeyDown}
            onBlur={() => commit(draft)}
            className="h-7 min-w-24 flex-1 bg-transparent font-mono text-sm text-t1 outline-none placeholder:text-t3"
          />
        </div>
        {action}
      </div>
    </FieldShell>
  );
}
