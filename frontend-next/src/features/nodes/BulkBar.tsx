import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Node } from "../../api/client";
import { useApiWrite } from "../../api/invalidation";
import { queries } from "../../api/keys";
import { confirm } from "../../components/confirm";
import { Button } from "../../components/ui/Button";
import { PICK_PROFILE, ProfileSelect } from "../../components/ui/ProfileSelect";
import { notifyError, notifyOk } from "../../components/ui/Toaster";
import { cn } from "../../lib/cn";
import { profileFromValue, profileName } from "../../lib/profiles";
import { SERVERS, type GroupKey } from "./list";
import { nodeMutationMessage } from "./nodeForm";

const BULK_KEY = ["nodes", "bulk"] as const;

/** The node a sequential bulk write stopped at, and why. */
class BulkStop extends Error {
  constructor(public node: Node, public cause: unknown) { super(node.name); }
}

async function eachInTurn(nodes: readonly Node[], write: (node: Node) => Promise<unknown>): Promise<number> {
  for (const node of nodes) {
    try {
      await write(node);
    } catch (error) {
      throw new BulkStop(node, error);
    }
  }
  return nodes.length;
}

function stopped(error: unknown, fallback: string): void {
  if (error instanceof BulkStop) notifyError(null, `${error.node.name}: ${nodeMutationMessage(error.cause, fallback)}`);
  else notifyError(error, fallback);
}

export interface BulkBarProps {
  group: GroupKey;
  /** The selected rows that are still shown. */
  selected: readonly Node[];
  onClear: () => void;
  className?: string;
}

/**
 * N18 / T6: what to do with the selected nodes — assign a tuning profile, detach them to Servers (a subscription's
 * nodes) or delete them (Servers), one node at a time, stopping at the first that fails.
 */
export function BulkBar({ group, selected, onClear, className }: BulkBarProps) {
  const queryClient = useQueryClient();
  const profiles = useQuery(queries.profiles());   // read for the select; nothing here polls it
  const updateNode = useApiWrite("updateNode");
  const deleteNode = useApiWrite("deleteNode");
  const detachNodes = useApiWrite("detachNodes");

  const assign = useMutation({
    mutationKey: BULK_KEY,
    mutationFn: ({ nodes, profileId }: { nodes: readonly Node[]; profileId: number | null; profileName: string }) =>
      eachInTurn(nodes, (node) => updateNode(node.id, { tuning_profile_id: profileId })),
    onSuccess: (count, { profileName }) => {
      notifyOk(`Assigned ${profileName} to ${count} node(s)`);
      onClear();
    },
    onError: (error) => stopped(error, "assign failed"),
  });
  const detach = useMutation({
    mutationKey: BULK_KEY,
    mutationFn: (nodes: readonly Node[]) => detachNodes(nodes.map((node) => node.id)).then(() => nodes.length),
    onSuccess: (count) => {
      notifyOk(`Detached ${count} node(s) to Servers`);
      onClear();
    },
    onError: (error) => notifyError(error, "detach failed"),
  });
  const remove = useMutation({
    mutationKey: BULK_KEY,
    mutationFn: (nodes: readonly Node[]) => eachInTurn(nodes, (node) => deleteNode(node.id)),
    onSuccess: (count) => {
      notifyOk(`Deleted ${count} server(s)`);
      onClear();
    },
    onError: (error) => stopped(error, "delete failed"),
  });

  const busy = assign.isPending || detach.isPending || remove.isPending;
  const count = selected.length;

  function onAssign(value: string) {
    if (value === PICK_PROFILE) return;
    const profileId = profileFromValue(value);
    assign.mutate({ nodes: selected, profileId, profileName: profileName(profiles.data, profileId) });
  }

  async function onDelete() {
    const nodes = [...selected];
    if (!(await confirm(`Delete ${nodes.length} server(s)?`, { confirmLabel: "Delete" }))) return;
    if (queryClient.isMutating({ mutationKey: BULK_KEY }) > 0) return;   // another bulk write started meanwhile
    remove.mutate(nodes);
  }

  return (
    <section
      aria-label="Selection"
      className={cn("glass sticky bottom-24 z-20 flex flex-wrap items-center gap-2 bg-solid p-2.5 md:bottom-4", className)}
    >
      <p aria-live="polite" className="px-1 text-sm font-semibold text-t1">{count} selected</p>
      <ProfileSelect
        aria-label="Assign tuning profile"
        profiles={profiles.data}
        placeholder="Assign profile…"
        value={PICK_PROFILE}
        disabled={busy}
        onChange={(event) => onAssign(event.target.value)}
        className="h-9"
      />
      {group === SERVERS ? (
        <Button size="sm" variant="danger" disabled={busy} onClick={() => void onDelete()}>{remove.isPending ? "Deleting…" : "Delete"}</Button>
      ) : (
        <Button size="sm" disabled={busy} onClick={() => detach.mutate(selected)}>{detach.isPending ? "Detaching…" : "Detach to Servers"}</Button>
      )}
      <Button size="sm" variant="ghost" disabled={busy} onClick={onClear} className="ml-auto">Clear</Button>
    </section>
  );
}
