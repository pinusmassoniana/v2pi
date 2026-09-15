import { useMutation, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useCallback, useMemo } from "react";
import { ApiError, type Status, type TuningProfile } from "../../api/client";
import { CONNECTION_BUSY, PROFILE_CONNECTION_WRITE, PROFILE_WRITE, isConnectionBusy, useApiWrite } from "../../api/invalidation";
import { keys, queries } from "../../api/keys";
import { confirm } from "../../components/confirm";
import { notifyError, notifyOk } from "../../components/ui/Toaster";
import { nodeLabel } from "../../lib/nodeHealth";
import { ACTIVE_NODE_CHANGED, NO_ACTIVE_NODE, applyActiveConfirmMessage, deleteProfileMessage } from "./profileForm";

/** Said when another profile write started while a question was open, so nothing was sent. */
export const PROFILE_BUSY = "Another profile change is still running — try again when it finishes";

/** Any tuning-profile write is running right now (for code that awaited a question first). */
export function isProfileBusy(client: QueryClient): boolean {
  return client.isMutating({ mutationKey: PROFILE_WRITE }) + client.isMutating({ mutationKey: PROFILE_CONNECTION_WRITE }) > 0;
}

/** The profile as the list holds it now: whether it is live may have changed while a question was open. */
function latest(client: QueryClient, profile: TuningProfile): TuningProfile {
  return client.getQueryData<TuningProfile[]>(keys.profiles)?.find((item) => item.id === profile.id) ?? profile;
}

/** What each profile row and card can do. Stable callbacks, so memoised rows keep them. */
export interface ProfileRowCallbacks {
  onEdit: (profile: TuningProfile) => void;
  onClone: (profile: TuningProfile) => void;
  onApply: (profile: TuningProfile) => void;
  onMakeDefault: (profile: TuningProfile) => void;
  onDelete: (profile: TuningProfile) => void;
}

/**
 * T2: apply to the active node, make default and delete. Apply and make default move the live tunnel, as does deleting
 * the live profile, so those are connection writes (PROFILE_CONNECTION_WRITE); any profile write disables every row
 * action (useProfileBusy). Edit and Clone come from the editor, which asks before discarding its edits.
 */
export function useProfileActions(editor: { edit: (profile: TuningProfile) => Promise<void>; clone: (profile: TuningProfile) => Promise<void>; onDeleted: (id: number) => void }): ProfileRowCallbacks {
  const queryClient = useQueryClient();
  const applyWrite = useApiWrite("applyProfileActive");
  const defaultWrite = useApiWrite("setDefaultProfile");
  const deleteWrite = useApiWrite("deleteProfile");
  const { edit, clone, onDeleted } = editor;

  const apply = useMutation({
    mutationKey: PROFILE_CONNECTION_WRITE,
    mutationFn: (profile: TuningProfile) => applyWrite(profile.id),
    onSuccess: (result) => notifyOk(`applied to active node ${result.node_id}`),
    onError: (error) => notifyError(error instanceof ApiError && error.status === 409 ? null : error, error instanceof ApiError && error.status === 409 ? NO_ACTIVE_NODE : "apply failed"),
  });
  const makeDefault = useMutation({
    mutationKey: PROFILE_CONNECTION_WRITE,
    mutationFn: (profile: TuningProfile) => defaultWrite(profile.id),
    onSuccess: () => notifyOk("default updated"),
    onError: (error) => notifyError(error, "set-default failed"),
  });
  const removeLive = useMutation({
    mutationKey: PROFILE_CONNECTION_WRITE,
    mutationFn: (profile: TuningProfile) => deleteWrite(profile.id),
    onSuccess: (_result, profile) => notifyOk(`deleted "${profile.name}"`),
    onError: (error) => notifyError(error, "delete failed"),
  });
  const removePlain = useMutation({
    mutationKey: PROFILE_WRITE,
    mutationFn: (profile: TuningProfile) => deleteWrite(profile.id),
    onSuccess: (_result, profile) => notifyOk(`deleted "${profile.name}"`),
    onError: (error) => notifyError(error, "delete failed"),
  });
  const { mutate: applyMutate } = apply;
  const { mutate: defaultMutate } = makeDefault;
  const { mutate: removeLiveMutate } = removeLive;
  const { mutate: removePlainMutate } = removePlain;

  const onApply = useCallback(async (profile: TuningProfile) => {
    const activeId = queryClient.getQueryData<Status>(keys.status)?.active_node_id ?? null;
    if (activeId === null) {
      notifyError(null, NO_ACTIVE_NODE);
      return;
    }
    const nodes = await queryClient.ensureQueryData(queries.nodes()).catch(() => undefined);
    if (!(await confirm(applyActiveConfirmMessage(profile.name, nodeLabel(nodes, activeId)), { confirmLabel: "Apply" }))) return;
    if (isConnectionBusy(queryClient) || isProfileBusy(queryClient)) {
      notifyError(null, isConnectionBusy(queryClient) ? CONNECTION_BUSY : PROFILE_BUSY);
      return;
    }
    // The status poll kept running while the question was open, and auto-failover may have moved the tunnel: the
    // gateway applies to whichever node is active when the request lands, so send it only to the node that was named.
    const nowActiveId = queryClient.getQueryData<Status>(keys.status)?.active_node_id ?? null;
    if (nowActiveId !== activeId) {
      notifyError(null, nowActiveId === null ? NO_ACTIVE_NODE : ACTIVE_NODE_CHANGED);
      return;
    }
    applyMutate(profile);
  }, [queryClient, applyMutate]);

  const onMakeDefault = useCallback((profile: TuningProfile) => defaultMutate(profile), [defaultMutate]);

  const onDelete = useCallback(async (profile: TuningProfile) => {
    if (!(await confirm(deleteProfileMessage(profile), { confirmLabel: "Delete" }))) return;
    const now = latest(queryClient, profile);
    if (isProfileBusy(queryClient) || (now.is_active && isConnectionBusy(queryClient))) {
      notifyError(null, isProfileBusy(queryClient) ? PROFILE_BUSY : CONNECTION_BUSY);
      return;
    }
    (now.is_active ? removeLiveMutate : removePlainMutate)(now, { onSuccess: () => onDeleted(now.id) });
  }, [queryClient, removeLiveMutate, removePlainMutate, onDeleted]);

  return useMemo(() => ({
    onEdit: (profile: TuningProfile) => void edit(profile),
    onClone: (profile: TuningProfile) => void clone(profile),
    onApply: (profile: TuningProfile) => void onApply(profile),
    onMakeDefault,
    onDelete: (profile: TuningProfile) => void onDelete(profile),
  }), [edit, clone, onApply, onMakeDefault, onDelete]);
}
