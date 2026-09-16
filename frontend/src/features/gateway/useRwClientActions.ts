import { useMutation, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useCallback, useMemo } from "react";
import { ApiError, api, errText, isNoAnswer, type Rw, type RwClient } from "../../api/client";
import { CONNECTION_BUSY, RW_WRITE, invalidateRefused, isConnectionBusy, saveRefusedMessage, useApiWrite } from "../../api/invalidation";
import { keys } from "../../api/keys";
import { confirm } from "../../components/confirm";
import { notifyError, notifyOk, notifyWarn } from "../../components/ui/Toaster";
import { copyDeferred } from "../../lib/clipboard";
import { downloadText } from "../../lib/download";
import { confirmTunnelStart } from "../../lib/tunnelStart";
import { NO_ANSWER } from "./networkForm";
import { removedMessage, suspendedMessage, type RwMessage } from "./revocation";

export const CLIENT_NOT_FOUND = "client not found";

/** Remove's question, word for word. */
export function removeClientMessage(name: string): string {
  return `Remove ${name}? Its link and config stop working immediately.`;
}

/** The client as the gateway's latest read holds it — gone, or changed, while a question was open. */
function latest(client: QueryClient, id: string): RwClient | undefined {
  return client.getQueryData<Rw>(keys.rw)?.clients.find((entry) => entry.id === id);
}

function say(message: RwMessage) {
  if (message.error) notifyError(null, message.text);
  else notifyOk(message.text);
}

/** What each client row and card can do. Stable callbacks, so memoised rows keep them. */
export interface RwClientCallbacks {
  onSuspend: (client: RwClient) => void;
  onResume: (client: RwClient) => void;
  onRemove: (client: RwClient) => void;
  onConfig: (client: RwClient) => void;
  onCopyLink: (client: RwClient) => void;
}

export interface RwClientActions extends RwClientCallbacks {
  /** Add a client; resolves true once the gateway added it (so the field can clear). */
  add: (name: string) => Promise<boolean>;
}

/**
 * A6: add, suspend, resume and remove a client — each a connection write (RW_WRITE) whose reply replaces the read, so a
 * revocation that could not be confirmed shows its banner at once — and the two reads, .conf and Copy link, which take
 * no key and never touch the cache. Adding and resuming grant access, so they ask first when that would start a tunnel
 * the operator stopped; Remove asks first. After any question: the busy flag and the client itself are read again.
 */
export function useRwClientActions({ onRemoved }: { onRemoved?: (client: RwClient) => void } = {}): RwClientActions {
  const queryClient = useQueryClient();
  const addWrite = useApiWrite("addRwClient");
  const enableWrite = useApiWrite("setRwClientEnabled");
  const deleteWrite = useApiWrite("deleteRwClient");

  /**
   * A write's failure. `outcome` names what a 502 did not do — only for the grants (add, resume), which re-apply inside
   * their transaction, so a refusal rolled them back. Suspend and remove commit before they revoke and never answer 502
   * for it, so should one arrive anyway it is reported as it came, never as "not removed".
   */
  const failed = (outcome: string | null, fallback: string) => (error: Error) => {
    if (error instanceof ApiError && error.status === 502) {
      notifyError(null, outcome ? saveRefusedMessage(error, outcome) : errText(error, fallback));
      void invalidateRefused(queryClient);
    } else if (error instanceof ApiError && error.status === 404) {
      notifyError(null, CLIENT_NOT_FOUND);
      void queryClient.invalidateQueries({ queryKey: keys.rw });
    } else if (isNoAnswer(error)) {
      notifyWarn(NO_ANSWER);
      void invalidateRefused(queryClient);
    } else {
      notifyError(error, fallback);
    }
  };
  const adopt = (reply: Rw) => queryClient.setQueryData(keys.rw, reply);

  const addMutation = useMutation({
    mutationKey: RW_WRITE,
    mutationFn: (name: string) => addWrite(name),
    onSuccess: (reply, name) => {
      adopt(reply);
      notifyOk(`added ${name}`);
    },
    onError: failed("not added", "add failed"),
  });
  const enableMutation = useMutation({
    mutationKey: RW_WRITE,
    mutationFn: ({ client, enabled }: { client: RwClient; enabled: boolean }) => enableWrite(client.id, enabled),
    onSuccess: (reply, { enabled }) => {
      adopt(reply);
      if (enabled) notifyOk("client resumed");
      else say(suspendedMessage(reply));
    },
    onError: (error, { enabled }) => failed(enabled ? "not resumed" : null, "update failed")(error),
  });
  const removeMutation = useMutation({
    mutationKey: RW_WRITE,
    mutationFn: (client: RwClient) => deleteWrite(client.id),
    onSuccess: (reply, client) => {
      adopt(reply);
      say(removedMessage(client.email, reply));
    },
    onError: failed(null, "remove failed"),
  });
  const { mutateAsync: addAsync } = addMutation;
  const { mutate: enable } = enableMutation;
  const { mutate: removeClient } = removeMutation;

  /** After a question: nothing else may have started, and the client must still be as it was. */
  const stillFine = useCallback((client: RwClient, enabled: boolean): boolean => {
    if (isConnectionBusy(queryClient)) {
      notifyError(null, CONNECTION_BUSY);
      return false;
    }
    const now = latest(queryClient, client.id);
    if (!now) {
      notifyError(null, CLIENT_NOT_FOUND);
      return false;
    }
    return now.enabled === enabled;
  }, [queryClient]);

  const add = useCallback(async (name: string) => {
    if (!(await confirmTunnelStart(queryClient))) return false;
    if (isConnectionBusy(queryClient)) {
      notifyError(null, CONNECTION_BUSY);
      return false;
    }
    return addAsync(name).then(() => true, () => false);
  }, [queryClient, addAsync]);

  const onSuspend = useCallback((client: RwClient) => enable({ client, enabled: false }), [enable]);

  const onResume = useCallback(async (client: RwClient) => {
    if (!(await confirmTunnelStart(queryClient))) return;
    if (stillFine(client, false)) enable({ client, enabled: true });
  }, [queryClient, stillFine, enable]);

  const onRemove = useCallback(async (client: RwClient) => {
    if (!(await confirm(removeClientMessage(client.email), { confirmLabel: "Remove" }))) return;
    const now = latest(queryClient, client.id);
    if (!stillFine(client, now?.enabled ?? client.enabled)) return;
    removeClient(client, { onSuccess: () => onRemoved?.(client) });
  }, [queryClient, stillFine, removeClient, onRemoved]);

  const onConfig = useCallback(async (client: RwClient) => {
    try {
      const { filename, config } = await api.rwClientConfig(client.id);
      downloadText(filename, config);
      notifyOk(`${filename} downloaded — import it in Shadowrocket`);
    } catch (error) {
      notifyError(error, "download failed");
    }
  }, []);

  const onCopyLink = useCallback(async (client: RwClient) => {
    try {
      // Started inside the click: the link is fetched and written in one go, and never shown.
      await copyDeferred(api.rwClientLink(client.id).then((reply) => reply.link));
      notifyOk("vless:// link copied");
    } catch (error) {
      notifyError(null, error instanceof ApiError ? errText(error, "copy failed") : "copy failed");
    }
  }, []);

  return useMemo(() => ({
    add,
    onSuspend,
    onResume: (client: RwClient) => void onResume(client),
    onRemove: (client: RwClient) => void onRemove(client),
    onConfig: (client: RwClient) => void onConfig(client),
    onCopyLink: (client: RwClient) => void onCopyLink(client),
  }), [add, onSuspend, onResume, onRemove, onConfig, onCopyLink]);
}
