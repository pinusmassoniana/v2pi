import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import { CONNECTION_BUSY, CONNECTION_WRITE, isConnectionBusy, useApiWrite } from "../api/invalidation";
import { confirm } from "../components/confirm";
import { notifyError, notifyOk } from "../components/ui/Toaster";

/** The Disconnect confirmation, word for word, wherever it is asked. */
export function disconnectMessage(name: string): string {
  return `Disconnect from ${name}? Devices lose the tunnel until a node is connected again.`;
}

/**
 * Disconnect, the same everywhere (Home's status block, a Nodes row, card, detail or form banner): ask first; since
 * another connection write may have started while the question was open, check again before sending; then
 * disconnect as a connection write (CONNECTION_WRITE), so every connection control waits for it.
 */
export function useDisconnect() {
  const queryClient = useQueryClient();
  const disconnectWrite = useApiWrite("disconnect");
  const { mutate, isPending } = useMutation({
    mutationKey: CONNECTION_WRITE,
    mutationFn: ({ id }: { id: number; name: string }) => disconnectWrite(id),
    onSuccess: (_result, { name }) => notifyOk(`Disconnected from ${name}`),
    onError: (error) => notifyError(error, "disconnect failed"),
  });
  const disconnect = useCallback(async (id: number, name: string) => {
    if (!(await confirm(disconnectMessage(name), { confirmLabel: "Disconnect" }))) return;
    if (isConnectionBusy(queryClient)) {
      notifyError(null, CONNECTION_BUSY);
      return;
    }
    mutate({ id, name });
  }, [queryClient, mutate]);
  return { disconnect, busy: isPending };
}
