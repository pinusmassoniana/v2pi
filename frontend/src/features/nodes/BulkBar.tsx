import { useIsMutating, useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api, type Node, type Status } from "../../api/client";
import { BULK_KEY, invalidate, useApiWrite, type MutationName } from "../../api/invalidation";
import { keys, queries } from "../../api/keys";
import { confirm } from "../../components/confirm";
import { Button } from "../../components/ui/Button";
import { PICK_PROFILE, ProfileSelect } from "../../components/ui/ProfileSelect";
import { ReadError } from "../../components/ui/States";
import { notifyError, notifyOk } from "../../components/ui/Toaster";
import { cn } from "../../lib/cn";
import { profileFromValue, profileName } from "../../lib/profiles";
import { SERVERS, type GroupKey } from "./list";
import { nodeMutationMessage } from "./nodeForm";


/** The node a sequential bulk write stopped at, and why. */
class BulkStop extends Error {
  constructor(public node: Node, public cause: unknown) { super(node.name); }
}

/**
 * Write each node in turn, stopping at the first failure. The lists the write changes are re-read once, when the
 * run ends or stops, not after every node.
 */
async function eachInTurn(
  client: QueryClient, name: MutationName, nodes: readonly Node[], write: (node: Node) => Promise<unknown>,
): Promise<number> {
  try {
    for (const node of nodes) {
      try {
        await write(node);
      } catch (error) {
        throw new BulkStop(node, error);
      }
    }
    return nodes.length;
  } finally {
    void invalidate(client, name);
  }
}

/** A bulk assign or delete: every selected node except the active one, which refuses both (409), and how many that left out. */
interface BulkRun { nodes: readonly Node[]; skipped: number }

function withoutActive(client: QueryClient, selected: readonly Node[]): BulkRun {
  const activeId = client.getQueryData<Status>(keys.status)?.active_node_id ?? null;
  const nodes = selected.filter((node) => node.id !== activeId);
  return { nodes, skipped: selected.length - nodes.length };
}

/** "· 1 skipped: active" after a result, when the active node was left out. */
function skippedNote(skipped: number): string {
  return skipped > 0 ? ` · ${skipped} skipped: active` : "";
}

function stopped(error: unknown, fallback: string): void {
  if (error instanceof BulkStop) notifyError(null, `${error.node.name}: ${nodeMutationMessage(error.cause, fallback)}`);
  else notifyError(error, fallback);
}

/**
 * The selection count for screen readers. Mounted with the list, before anything is selected — a live region that
 * appears together with its first text is not announced — so the first "1 selected" is read too.
 */
export function SelectionAnnouncer({ count }: { count: number }) {
  return <p aria-live="polite" data-selection-count={count} className="sr-only">{count > 0 ? `${count} selected` : ""}</p>;
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
 * nodes) or delete them (Servers), one node at a time, stopping at the first that fails. Assign and delete skip the
 * active node, which refuses both, and say so.
 */
export function BulkBar({ group, selected, onClear, className }: BulkBarProps) {
  const queryClient = useQueryClient();
  const profiles = useQuery(queries.profiles());   // read for the select; nothing here polls it
  const detachNodes = useApiWrite("detachNodes");

  // What a run did is said even when the bar has gone; clearing the selection is passed to mutate, which runs only
  // while this bar is still mounted — a run finishing after a group switch must not wipe the selection made there.
  const assign = useMutation({
    mutationKey: BULK_KEY,
    mutationFn: ({ nodes, profileId }: BulkRun & { profileId: number | null; profileName: string }) =>
      eachInTurn(queryClient, "updateNode", nodes, (node) => api.updateNode(node.id, { tuning_profile_id: profileId })),
    onSuccess: (count, { profileName, skipped }) => notifyOk(`Assigned ${profileName} to ${count} node(s)${skippedNote(skipped)}`),
    onError: (error) => stopped(error, "assign failed"),
  });
  const detach = useMutation({
    mutationKey: BULK_KEY,
    mutationFn: (nodes: readonly Node[]) => detachNodes(nodes.map((node) => node.id)).then(() => nodes.length),
    onSuccess: (count) => notifyOk(`Detached ${count} node(s) to Servers`),
    onError: (error) => notifyError(error, "detach failed"),
  });
  const remove = useMutation({
    mutationKey: BULK_KEY,
    mutationFn: ({ nodes }: BulkRun) => eachInTurn(queryClient, "deleteNode", nodes, (node) => api.deleteNode(node.id)),
    onSuccess: (count, { skipped }) => notifyOk(`Deleted ${count} server(s)${skippedNote(skipped)}`),
    onError: (error) => stopped(error, "delete failed"),
  });

  // The shared key, not these instances' own state: a bulk write keeps the bar locked even if it remounts mid-run.
  const busy = useIsMutating({ mutationKey: BULK_KEY }) > 0;
  const count = selected.length;

  function onAssign(value: string) {
    if (value === PICK_PROFILE) return;
    const profileId = profileFromValue(value);
    assign.mutate({ ...withoutActive(queryClient, selected), profileId, profileName: profileName(profiles.data, profileId) }, { onSuccess: onClear });
  }

  async function onDelete() {
    const chosen = [...selected];
    if (!(await confirm(`Delete ${withoutActive(queryClient, chosen).nodes.length} server(s)?`, { confirmLabel: "Delete" }))) return;
    if (queryClient.isMutating({ mutationKey: BULK_KEY }) > 0) return;   // another bulk write started meanwhile
    remove.mutate(withoutActive(queryClient, chosen), { onSuccess: onClear });   // the active node as it is now, after the question
  }

  return (
    <section
      aria-label="Selection"
      className={cn("glass sticky bottom-24 z-20 flex flex-wrap items-center gap-2 bg-solid p-2.5 md:bottom-4", className)}
    >
      <p className="px-1 text-sm font-semibold text-t1">{count} selected</p>
      <ProfileSelect
        aria-label="Assign tuning profile"
        profiles={profiles.data}
        placeholder="Assign profile…"
        value={PICK_PROFILE}
        disabled={busy}
        onChange={(event) => onAssign(event.target.value)}
        className="h-9"
      />
      <ReadError query={profiles} message="Profiles did not load" />
      {group === SERVERS ? (
        <Button size="sm" variant="danger" disabled={busy} onClick={() => void onDelete()}>{remove.isPending ? "Deleting…" : "Delete"}</Button>
      ) : (
        <Button size="sm" disabled={busy} onClick={() => detach.mutate(selected, { onSuccess: onClear })}>{detach.isPending ? "Detaching…" : "Detach to Servers"}</Button>
      )}
      <Button size="sm" variant="ghost" disabled={busy} onClick={onClear} className="ml-auto">Clear</Button>
    </section>
  );
}
