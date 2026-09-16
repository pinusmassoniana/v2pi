import { zodResolver } from "@hookform/resolvers/zod";
import { useCallback, useEffect } from "react";
import { useForm, type UseFormReturn } from "react-hook-form";
import type { Rw } from "../../api/client";
import { useUnsavedGuard } from "../../app/guard";
import { rwFormSchema, rwToForm, type RwFormValues } from "./rwForm";

export interface RwFormState {
  form: UseFormReturn<RwFormValues>;
  dirty: boolean;
  /** The gateway's saved inbound, hosts or subnets moved since the edit started: Discard loads them. */
  gatewayChanged: boolean;
  /** Put the gateway's latest values back, without asking; the private key goes blank. */
  discard: () => void;
}

/**
 * The inbound form over the remote-access read — every editable field, never the clients. It follows the gateway while
 * nothing is edited and never reseeds once something is (Save replaces everything with what is on screen). The typed
 * private key lives only here: never in the query cache or any storage, and blanked when the screen goes.
 */
export function useRwForm(rw: Rw): RwFormState {
  const form = useForm<RwFormValues>({ resolver: zodResolver(rwFormSchema(rw.has_private_key)), defaultValues: rwToForm(rw), mode: "onChange" });
  const { reset, setValue, formState: { isDirty, defaultValues } } = form;
  useUnsavedGuard(isDirty);

  useEffect(() => {
    if (!isDirty) reset(rwToForm(rw));
  }, [rw, isDirty, reset]);

  useEffect(() => () => setValue("privateKey", ""), [setValue]);

  const latest = rwToForm(rw);
  const gatewayChanged = isDirty && (Object.keys(latest) as (keyof RwFormValues)[])
    .some((field) => field !== "privateKey" && JSON.stringify(defaultValues?.[field]) !== JSON.stringify(latest[field]));
  const discard = useCallback(() => reset(rwToForm(rw)), [rw, reset]);
  return { form, dirty: isDirty, gatewayChanged, discard };
}
