import { Plus, X } from "lucide-react";
import { useFieldArray, useWatch, type Control, type FieldErrors, type UseFormRegister } from "react-hook-form";
import { Button } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import { Select } from "../../components/ui/Select";
import { cn } from "../../lib/cn";
import { MAX_NOISES, NEW_NOISE, NOISE_TYPES, type ProfileFormValues } from "./profileForm";

export interface NoiseRowsProps {
  control: Control<ProfileFormValues>;
  register: UseFormRegister<ProfileFormValues>;
  errors: FieldErrors<ProfileFormValues>;
}

/** T3 UDP noise: up to 32 rows of type, packet and delay, each labelled by its number; rows keep their inputs when one above goes. */
export function NoiseRows({ control, register, errors }: NoiseRowsProps) {
  const { fields, append, remove } = useFieldArray({ control, name: "noises" });
  const types = useWatch({ control, name: "noises" });
  return (
    <div className="flex flex-col gap-2">
      {fields.length > 0 ? (
        <div className="grid grid-cols-[5.5rem_minmax(0,1fr)_minmax(0,1fr)_2rem] gap-x-2 px-0.5 text-[10.5px] font-semibold text-t3" aria-hidden>
          <span>Type</span><span>Packet</span><span>Delay · ms</span><span />
        </div>
      ) : null}
      {fields.map((field, index) => {
        const n = index + 1;
        const rowErrors = errors.noises?.[index];
        const problem = rowErrors?.packet?.message ?? rowErrors?.delay?.message;
        return (
          <div key={field.id} data-noise-row={n} className="flex flex-col gap-1">
            <div className="grid grid-cols-[5.5rem_minmax(0,1fr)_minmax(0,1fr)_2rem] items-center gap-2">
              <Select aria-label={`Noise ${n} type`} className="h-9 px-2" {...register(`noises.${index}.type`)}>
                {NOISE_TYPES.map((type) => <option key={type} value={type}>{type}</option>)}
              </Select>
              <Input
                aria-label={`Noise ${n} packet`}
                placeholder={types?.[index]?.type === "rand" ? "50-150" : "packet"}
                aria-invalid={rowErrors?.packet ? true : undefined}
                className={cn("h-9 font-mono text-xs", rowErrors?.packet && "border-bad/60")}
                {...register(`noises.${index}.packet`)}
              />
              <Input
                aria-label={`Noise ${n} delay`}
                placeholder="10-16"
                aria-invalid={rowErrors?.delay ? true : undefined}
                className={cn("h-9 font-mono text-xs", rowErrors?.delay && "border-bad/60")}
                {...register(`noises.${index}.delay`)}
              />
              <Button size="icon" variant="ghost" className="size-8" aria-label={`Remove noise ${n}`} onClick={() => remove(index)}>
                <X size={15} aria-hidden />
              </Button>
            </div>
            {problem ? <p className="text-[11px] text-bad">Noise {n} {problem}</p> : null}
          </div>
        );
      })}
      <div className="flex items-center gap-3">
        <Button size="sm" variant="ghost" className="border border-dashed border-line" disabled={fields.length >= MAX_NOISES} onClick={() => append({ ...NEW_NOISE })}>
          <Plus size={14} aria-hidden />Add noise
        </Button>
        <span className="text-[11px] text-t3">{fields.length} / {MAX_NOISES}</span>
      </div>
    </div>
  );
}
