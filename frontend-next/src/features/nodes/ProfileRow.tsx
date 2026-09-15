import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { Node } from "../../api/client";
import { useApiWrite } from "../../api/invalidation";
import { queries } from "../../api/keys";
import { Button } from "../../components/ui/Button";
import { notifyError, notifyOk } from "../../components/ui/Toaster";
import { cn } from "../../lib/cn";
import { DISCONNECT_FIRST } from "./NodeRowActions";
import { nodeMutationMessage, profileFromValue, profileValue } from "./nodeForm";
import { SELECT_CLASS } from "./ServersToolbar";

/** T6 / N13: the node's tuning profile, changed in place. The active node refuses edits (409), so it is disabled there. */
export function ProfileRow({ node, active }: { node: Node; active: boolean }) {
  const profiles = useQuery(queries.profiles());   // read for the select; nothing here polls it
  const update = useApiWrite("updateNode");
  const [editing, setEditing] = useState(false);
  const name = (id: number | null) => {
    if (id === null) {
      const fallback = profiles.data?.find((profile) => profile.is_default)?.name;
      return fallback ? `(default) · ${fallback}` : "(default)";
    }
    return profiles.data?.find((profile) => profile.id === id)?.name ?? `profile #${id}`;
  };
  const change = useMutation({
    mutationFn: (profileId: number | null) => update(node.id, { tuning_profile_id: profileId }),
    onSuccess: (_result, profileId) => {
      notifyOk(`Assigned ${profileId === null ? "(global default)" : name(profileId)} to ${node.name}`);
      setEditing(false);
    },
    onError: (error) => notifyError(null, nodeMutationMessage(error, "profile change failed")),
  });

  return (
    <section aria-label="Tuning profile" className="glass flex flex-wrap items-center gap-2.5 p-3.5">
      <div className="min-w-0 flex-1">
        <h3 className="text-[10.5px] font-semibold uppercase tracking-[.07em] text-t3">Tuning profile</h3>
        {editing ? (
          <select
            aria-label="Tuning profile"
            autoFocus
            defaultValue={profileValue(node.tuning_profile_id)}
            disabled={change.isPending}
            onChange={(event) => change.mutate(profileFromValue(event.target.value))}
            className={cn(SELECT_CLASS, "mt-1 w-full")}
          >
            <option value="">(default)</option>
            {profiles.data?.map((profile) => <option key={profile.id} value={String(profile.id)}>{profile.name}</option>)}
          </select>
        ) : (
          <p className="mt-0.5 truncate text-[13px] font-semibold text-t1">{name(node.tuning_profile_id)}</p>
        )}
      </div>
      <div className="flex flex-col items-end">
        <Button size="sm" disabled={active || change.isPending} onClick={() => setEditing(!editing)}>{editing ? "Cancel" : "Change"}</Button>
        {active ? <span className="mt-0.5 text-[11px] text-t3">{DISCONNECT_FIRST}</span> : null}
      </div>
    </section>
  );
}
