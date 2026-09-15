import { Plus, X } from "lucide-react";
import { useFieldArray, type Control, type UseFormRegister } from "react-hook-form";
import { Button } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import type { SubFormValues } from "./subForm";

export interface KeyValueRowsEditorProps {
  name: "headers" | "queries";
  control: Control<SubFormValues>;
  register: UseFormRegister<SubFormValues>;
  /** "Headers" / "Query params". */
  legend: string;
  /** "Header" / "Query param": names each row's inputs and buttons. */
  item: string;
  addLabel: string;
  keyPlaceholder: string;
  empty: string;
}

/**
 * U5 / U7: name → value rows. Each row keeps a stable id (useFieldArray's), so removing one never hands its inputs —
 * caret, composition, autofill — to the row that moves into its place.
 */
export function KeyValueRowsEditor({ name, control, register, legend, item, addLabel, keyPlaceholder, empty }: KeyValueRowsEditorProps) {
  const { fields, append, remove } = useFieldArray({ control, name });
  return (
    <fieldset className="flex flex-col gap-2 rounded-2xl border border-line bg-glass p-3">
      <legend className="px-1 text-[10.5px] font-semibold uppercase tracking-[.07em] text-t3">{legend}</legend>
      {fields.length === 0 ? <p className="text-xs text-t3">{empty}</p> : null}
      {fields.map((field, index) => (
        <div key={field.id} className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_2.25rem] gap-1.5">
          <Input aria-label={`${item} ${index + 1} name`} placeholder={keyPlaceholder} className="h-9 font-mono text-xs" {...register(`${name}.${index}.key`)} />
          <Input aria-label={`${item} ${index + 1} value`} placeholder="value" className="h-9 font-mono text-xs" {...register(`${name}.${index}.value`)} />
          <Button size="icon" variant="ghost" aria-label={`Remove ${item.toLowerCase()} ${index + 1}`} onClick={() => remove(index)}>
            <X size={14} aria-hidden />
          </Button>
        </div>
      ))}
      <Button size="sm" variant="ghost" className="self-start" onClick={() => append({ key: "", value: "" })}>
        <Plus size={14} aria-hidden />{addLabel}
      </Button>
    </fieldset>
  );
}
