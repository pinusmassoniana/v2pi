import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { ApiError, isNoAnswer, type Rw, type RwIn } from "../../api/client";
import { CONNECTION_BUSY, RW_WRITE, invalidateRefused, isConnectionBusy, saveRefusedMessage, useApiWrite } from "../../api/invalidation";
import { keys } from "../../api/keys";
import { notifyError, notifyOk, notifyWarn } from "../../components/ui/Toaster";
import { confirmTunnelStart, knownStatus } from "../../lib/tunnelStart";
import { NO_ANSWER } from "./networkForm";
import { rwSavedMessage } from "./revocation";
import { formToRwIn, provablyNarrows, rwToForm, type RwFormValues } from "./rwForm";
import type { RwFormState } from "./useRwForm";

interface SaveVariables {
  /** A node was active when Save was pressed: a widening save is rebuilt into the live config now. */
  activeAtSend: boolean;
}

/** The backend's name for each inbound field, as a schema refusal's `loc` gives it. */
const FIELDS: Readonly<Record<string, keyof RwFormValues>> = {
  enabled: "enabled", port: "port", dest: "dest", server_names: "serverNames", short_ids: "shortIds", public_key: "publicKey",
  endpoint: "endpoint", private_key: "privateKey", hosts: "hosts", routed_nets: "routedNets",
};

/** A schema refusal's fields and messages (never its `input`, which can echo a key back). */
function schemaIssues(detail: unknown): { field: keyof RwFormValues; message: string }[] {
  if (!Array.isArray(detail)) return [];
  return detail.flatMap((item) => {
    const row = item as { loc?: unknown; msg?: unknown };
    const field = Array.isArray(row.loc) ? FIELDS[String(row.loc[1])] : undefined;
    return field && typeof row.msg === "string" ? [{ field, message: row.msg.replace(/^Value error, /, "") }] : [];
  });
}

export interface RwSave {
  save: () => Promise<void>;
  saving: boolean;
  /** A refusal that names no field, for the toolbar's status line. */
  formError: string | null;
}

/**
 * A4 Save: the whole inbound form as one full-replace PUT /rw, a connection write (RW_WRITE). A save that provably
 * narrows access revokes and never starts xray; any other one asks first when it would start a tunnel the operator
 * stopped (decision 3), then checks that no other connection write started meanwhile.
 *
 * Deviation from the brief: the brief's `save` is `form.handleSubmit(async (values) => { ...; mutation.mutate({ body:
 * formToRwIn(values), activeAtSend }) })`, which makes the full body — the private key included — a mutation variable.
 * TanStack keeps a mutation's `state.variables` in the MutationCache for its gcTime (default 5 min) after it settles,
 * so the key would sit there reachable long after the form blanked it — spec §5.8 says "the private key only in the
 * form". Here the body travels through a ref instead (`pendingBody`), read once by `mutationFn` and cleared the
 * moment it is; the only mutation variable is `activeAtSend`. The ref is written from a plain `save` function — not
 * one handed to `form.handleSubmit`, which is called at render time to build the bound submit function and so cannot
 * be proven by the ref-safety check to run after render — so `save` instead calls `form.trigger()` (the same
 * resolver-driven validation `handleSubmit` runs) and reads `form.getValues()` itself once the form is valid.
 */
export function useRwSave(rw: Rw, state: RwFormState): RwSave {
  const queryClient = useQueryClient();
  const putRw = useApiWrite("putRw");
  const [formError, setFormError] = useState<string | null>(null);
  const { form } = state;
  const pendingBody = useRef<RwIn | null>(null);

  const mutation = useMutation<Rw, Error, SaveVariables>({
    mutationKey: RW_WRITE,
    mutationFn: () => {
      const body = pendingBody.current;
      pendingBody.current = null;
      if (!body) throw new Error("useRwSave: save fired with no pending body");
      return putRw(body);
    },
    onSuccess: (saved, { activeAtSend }) => {
      const message = rwSavedMessage(saved, activeAtSend);
      if (message.error) notifyError(null, message.text);
      else notifyOk(message.text);
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 502) {
        // A widening save re-applies inside its transaction: the refusal rolled it back.
        notifyError(null, saveRefusedMessage(error));
        void invalidateRefused(queryClient);
      } else if (isNoAnswer(error)) {
        notifyWarn(NO_ANSWER);
        void invalidateRefused(queryClient);
      } else if (!(error instanceof ApiError && error.status === 422)) {
        notifyError(error, "save failed");
      }
    },
  });

  async function save(): Promise<void> {
    setFormError(null);
    if (!(await form.trigger())) return;
    const values = form.getValues();
    if (!provablyNarrows(rw, values) && !(await confirmTunnelStart(queryClient))) return;
    if (isConnectionBusy(queryClient)) {
      notifyError(null, CONNECTION_BUSY);
      return;
    }
    const activeAtSend = (knownStatus(queryClient)?.active_node_id ?? null) !== null;
    pendingBody.current = formToRwIn(values);
    mutation.mutate({ activeAtSend }, {
      onSuccess: (saved: Rw) => {
        queryClient.setQueryData(keys.rw, saved);
        form.reset(rwToForm(saved));
      },
      onError: (error) => {
        if (!(error instanceof ApiError && error.status === 422)) return;
        const issues = schemaIssues(error.detail);
        if (issues.length === 0) setFormError(error.message);
        for (const issue of issues) form.setError(issue.field, { type: "server", message: issue.message });
      },
    });
  }

  return { save, saving: mutation.isPending, formError };
}
