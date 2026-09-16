import { useId } from "react";
import { fmtBytes } from "../../lib/format";
import { cn } from "../../lib/cn";

export interface FilePickerProps {
  /** What the control reads and is named while nothing is picked. */
  label: string;
  /** What it reads once a file is picked ("Change…"). */
  changeLabel?: string;
  accept?: string;
  file: File | null;
  onPick: (file: File | null) => void;
  disabled?: boolean;
  className?: string;
}

/**
 * Pick one local file, and say which one: a real `<input type="file">` named by a label styled as a button, then the
 * filename and its size. The input stays in the accessibility tree (it is only visually hidden), so it is reachable by
 * keyboard and by name — a `<button>` that clicks a hidden input would not be.
 *
 * System-only until something else needs it: Restore and the settings import both read a file in the browser, check it
 * before anything is sent, and never upload it as a form.
 */
export function FilePicker({ label, changeLabel = "Change…", accept = "application/json,.json", file, onPick, disabled, className }: FilePickerProps) {
  const id = useId();
  return (
    <div className={cn("flex min-w-0 flex-wrap items-center gap-2", className)}>
      <label
        htmlFor={id}
        className={cn(
          "inline-flex h-9 shrink-0 cursor-pointer items-center rounded-xl border border-line bg-glass-2 px-3.5 text-[13px] font-semibold text-t1",
          "has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-g2",
          disabled ? "cursor-not-allowed opacity-50" : "hover:bg-glass",
        )}
      >
        {file ? changeLabel : label}
        <input
          id={id}
          type="file"
          accept={accept}
          disabled={disabled}
          className="sr-only"
          // Cleared so picking the same file twice still fires a change — the operator may have edited it.
          onChange={(event) => { const picked = event.target.files?.[0] ?? null; event.target.value = ""; onPick(picked); }}
        />
      </label>
      {file ? (
        <>
          <span className="min-w-0 truncate font-mono text-[12px] text-t1">{file.name}</span>
          <span className="shrink-0 text-[11px] text-t3">{fmtBytes(file.size)}</span>
        </>
      ) : null}
    </div>
  );
}
