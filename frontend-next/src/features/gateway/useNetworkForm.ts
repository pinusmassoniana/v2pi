import { zodResolver } from "@hookform/resolvers/zod";
import { useCallback, useEffect } from "react";
import { useForm, type UseFormReturn } from "react-hook-form";
import type { Network } from "../../api/client";
import { useUnsavedGuard } from "../../app/guard";
import { networkFormSchema, networkToForm, type NetworkField, type NetworkFormValues } from "./networkForm";

export interface NetworkFormState {
  form: UseFormReturn<NetworkFormValues>;
  dirty: boolean;
  /** The gateway's saved editable fields moved since the edit started: Discard loads them. */
  gatewayChanged: boolean;
  /** Put the gateway's latest values back, without asking. */
  discard: () => void;
}

/**
 * The segment form over the gateway's network read. While nothing is edited it follows every poll; once something is,
 * it never reseeds — Apply sends the whole form, so what is sent must be exactly what is on screen — and it says when
 * the gateway's own values moved since the edit started (the form's default values are that starting point).
 */
export function useNetworkForm(network: Network): NetworkFormState {
  const form = useForm<NetworkFormValues>({ resolver: zodResolver(networkFormSchema), defaultValues: networkToForm(network), mode: "onChange" });
  const { reset, formState: { isDirty, defaultValues } } = form;
  useUnsavedGuard(isDirty);

  useEffect(() => {
    if (!isDirty) reset(networkToForm(network));
  }, [network, isDirty, reset]);

  const latest = networkToForm(network);
  const gatewayChanged = isDirty && (Object.keys(latest) as NetworkField[]).some((field) => defaultValues?.[field] !== latest[field]);
  const discard = useCallback(() => reset(networkToForm(network)), [network, reset]);
  return { form, dirty: isDirty, gatewayChanged, discard };
}
