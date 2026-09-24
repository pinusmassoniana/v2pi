import { useMutation, useMutationState, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ApiError, isNoAnswer, type Network, type NetworkPatch } from "../../api/client";
import { CONNECTION_BUSY, NETWORK_WRITE, invalidateRefused, isConnectionBusy, useApiWrite } from "../../api/invalidation";
import { keys } from "../../api/keys";
import { confirm } from "../../components/confirm";
import { notifyError, notifyOk, notifyWarn } from "../../components/ui/Toaster";
import { knownStatus } from "../../lib/tunnelStart";
import {
  NO_ANSWER, RESTART_CONFIRM, appliedMessage, applyRefusedMessage, detailField, formToNetworkPatch, networkToForm, poolIssue, restartConfirmNeeded,
} from "./networkForm";
import type { NetworkFormState } from "./useNetworkForm";

interface ApplyVariables {
  patch: Required<NetworkPatch>;
  /** IPv6 was flipped while the tunnel ran: the gateway restarts it. */
  ipv6Restarted: boolean;
}

export interface NetworkApply {
  apply: () => Promise<void>;
  /** An Apply runs: the fields are locked and the progress shows. */
  applying: boolean;
  /** When the running Apply started, however many times the screen was left and opened meanwhile. */
  startedAt: number | null;
  /** A refusal that belongs to no single field (the cross-field checks), for the footer's status line. */
  formError: string | null;
}

/**
 * W4 Apply to host: the whole form, as one connection write (NETWORK_WRITE) with a long timeout. It asks first only when
 * the apply restarts the tunnel (decision 4), and checks again that no other connection write started while the
 * question was open. A refusal (502) or no answer keeps the edits and re-reads what the apply can have moved.
 */
export function useNetworkApply(network: Network, state: NetworkFormState): NetworkApply {
  const queryClient = useQueryClient();
  const putNetwork = useApiWrite("putNetwork");
  const [formError, setFormError] = useState<string | null>(null);
  const { form } = state;
  // Whether the screen that started an apply is still here. A 422 is shown under its field by the per-call handler
  // below, which dies with the screen — so a refusal that lands after leaving is said by the hook instead.
  const shown = useRef(true);
  useEffect(() => {
    shown.current = true;
    return () => { shown.current = false; };
  }, []);

  // Messages come from here, so they are said even when the screen was left while the apply ran.
  const mutation = useMutation({
    mutationKey: NETWORK_WRITE,
    mutationFn: ({ patch }: ApplyVariables) => putNetwork(patch),
    onSuccess: (_saved, { ipv6Restarted }) => notifyOk(appliedMessage(ipv6Restarted)),
    onError: (error) => {
      if (error instanceof ApiError && error.status === 502) {
        const refused = applyRefusedMessage(error.message);
        notifyError(null, refused.text, { sticky: refused.sticky });
        void invalidateRefused(queryClient);
      } else if (isNoAnswer(error)) {
        notifyWarn(NO_ANSWER);
        void invalidateRefused(queryClient);
      } else if (!(error instanceof ApiError && error.status === 422)) {
        notifyError(error, "apply failed");
      } else if (!shown.current) {
        notifyError(error, "apply refused");
      }
    },
  });
  const running = useMutationState({
    filters: { mutationKey: NETWORK_WRITE, status: "pending" },
    select: (entry) => entry.state.submittedAt,
  });

  const apply = form.handleSubmit(async (values) => {
    setFormError(null);
    if (poolIssue(values)) return;
    if (restartConfirmNeeded(network, values, knownStatus(queryClient))) {
      if (!(await confirm(RESTART_CONFIRM, { confirmLabel: "Apply to host", danger: false }))) return;
    }
    // Another connection write may have started while the question was open (or since the button was drawn).
    if (isConnectionBusy(queryClient)) {
      notifyError(null, CONNECTION_BUSY);
      return;
    }
    const status = knownStatus(queryClient);
    const connected = status !== undefined && status.running && status.active_node_id !== null;
    mutation.mutate({ patch: formToNetworkPatch(values), ipv6Restarted: connected && values.ipv6 !== network.ipv6_enabled }, {
      onSuccess: (saved) => {
        queryClient.setQueryData(keys.network, saved);
        form.reset(networkToForm(saved));
      },
      onError: (error) => {
        if (!(error instanceof ApiError && error.status === 422)) return;
        const field = detailField(error.message);
        if (field) form.setError(field, { type: "server", message: error.message.replace(/^body\./, "") }, { shouldFocus: true });
        else setFormError(error.message);
      },
    });
  });

  return { apply, applying: running.length > 0, startedAt: running[0] ?? null, formError };
}
