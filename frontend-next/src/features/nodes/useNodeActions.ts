import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Node, NodeHealth } from "../../api/client";
import { CONNECTION_BUSY, CONNECTION_WRITE, useApiWrite, useConnectionBusy } from "../../api/invalidation";
import { keys, queries } from "../../api/keys";
import { confirm } from "../../components/confirm";
import { notifyError, notifyOk } from "../../components/ui/Toaster";
import { useDisconnect } from "../../lib/disconnect";
import { deleteNodeMessage, mergeHealth } from "./list";
import { nodeMutationMessage } from "./nodeForm";

export const OFFLINE_HINT = "Gateway unreachable";

/**
 * N6: Connect / Disconnect one node. A connection write: disabled while any connection write runs (here, on Home or
 * in ⌘K) and while the gateway is unreachable, with the reason to show beside the control. Disconnect asks first,
 * as on Home (useDisconnect); Connect does not.
 */
export function useNodeConnection(node: Pick<Node, "id" | "name">, active: boolean) {
  const apply = useApiWrite("apply");
  const disconnect = useDisconnect();
  const offline = useQuery(queries.status()).isError;   // the shell polls status; this reads the cache
  const connectionBusy = useConnectionBusy();
  const connect = useMutation({
    mutationKey: CONNECTION_WRITE,
    mutationFn: () => apply(node.id),
    onSuccess: () => notifyOk(`Connected to ${node.name}`),
    onError: (error) => notifyError(error, "connect failed"),
  });
  const busy = connect.isPending || disconnect.busy;
  const reason = offline ? OFFLINE_HINT : connectionBusy && !busy ? CONNECTION_BUSY : null;
  return {
    busy,
    disabled: offline || connectionBusy,
    reason,
    label: busy ? (active ? "Disconnecting…" : "Connecting…") : active ? "Disconnect" : "Connect",
    toggle: () => (active ? void disconnect.disconnect(node.id, node.name) : connect.mutate()),
  };
}

/** N7: test one node (TCP + HTTP + real through a throwaway xray); its result lands in the list at once. */
export function useNodeTest(node: Pick<Node, "id" | "name">) {
  const probe = useApiWrite("probeNode");
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => probe(node.id),
    onSuccess: (result) => queryClient.setQueryData<NodeHealth[]>(keys.nodeHealth, (old) => mergeHealth(old, result)),
    onError: (error) => notifyError(error, `test of ${node.name} failed`),
  });
  return { busy: mutation.isPending, run: () => mutation.mutate() };
}

/** N15 delete (Servers, with the contract's confirmation) and detach to Servers (subscription nodes). */
export function useNodeRemoval() {
  const deleteWrite = useApiWrite("deleteNode");
  const detachWrite = useApiWrite("detachNodes");
  const remove = useMutation({
    mutationFn: (node: Pick<Node, "id" | "name">) => deleteWrite(node.id),
    onSuccess: (_result, node) => notifyOk(`Deleted ${node.name}`),
    onError: (error) => notifyError(null, nodeMutationMessage(error, "delete failed")),
  });
  const detach = useMutation({
    mutationFn: (node: Pick<Node, "id" | "name">) => detachWrite([node.id]),
    onSuccess: (_result, node) => notifyOk(`Detached ${node.name} to Servers`),
    onError: (error) => notifyError(error, "detach failed"),
  });
  return {
    busy: remove.isPending || detach.isPending,
    /** Resolves true once the node is deleted; false when the confirmation was declined or the delete failed. */
    async deleteNode(node: Pick<Node, "id" | "name" | "address">): Promise<boolean> {
      if (!(await confirm(deleteNodeMessage(node), { confirmLabel: "Delete" }))) return false;
      try {
        await remove.mutateAsync(node);
        return true;
      } catch {
        return false;
      }
    },
    detachNode: (node: Pick<Node, "id" | "name">) => detach.mutateAsync(node).then(() => true, () => false),
  };
}
