import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { Node } from "../../api/client";
import { useApiWrite } from "../../api/invalidation";
import { queries } from "../../api/keys";
import { Button } from "../../components/ui/Button";
import { ProfileSelect } from "../../components/ui/ProfileSelect";
import { ReadError } from "../../components/ui/States";
import { notifyError, notifyOk } from "../../components/ui/Toaster";
import { GLOBAL_DEFAULT, profileFromValue, profileName, profileValue } from "../../lib/profiles";
import { DISCONNECT_FIRST } from "./NodeRowActions";
import { nodeMutationMessage } from "./nodeForm";

/**
 * B4: which of a profile's knobs a node can actually use. The selector stays enabled for every
 * protocol — fragmentation and noise are the anti-DPI knobs that matter most on a censored link,
 * and they ride a dialerProxy outbound that works under any of them. What does NOT apply is
 * named instead of being silently ignored.
 */
export const PROFILE_SCOPE: Record<string, string> = {
  trojan: "XHTTP padding and xmux do not apply to a Trojan node; fragmentation, noise, mux, DoH and QUIC do.",
  shadowsocks: "Only fragmentation, noise, DoH and QUIC apply to a Shadowsocks node — it has no TLS layer to "
    + "fingerprint and xray's mux does not ride it.",
};

/**
 * T6 / N13: the node's tuning profile, changed in place. Picking a profile only stages it — arrow keys on a closed
 * select change its value too — and Save (or Enter) sends it. The active node refuses edits (409), so the select and
 * Save are disabled there, also when the node becomes active while the row is open.
 */
export function ProfileRow({ node, active }: { node: Node; active: boolean }) {
  const profiles = useQuery(queries.profiles());   // read for the select; nothing here polls it
  const update = useApiWrite("updateNode");
  const [editing, setEditing] = useState(false);
  // What was picked, or null for "not changed yet": the select then shows the node's current profile, including
  // one that only has an option once the profiles load, or that a refresh changed meanwhile.
  const [picked, setPicked] = useState<string | null>(null);
  const change = useMutation({
    mutationFn: (profileId: number | null) => update(node.id, { tuning_profile_id: profileId }),
    onSuccess: (_result, profileId) => {
      notifyOk(`Assigned ${profileName(profiles.data, profileId)} to ${node.name}`);
      setEditing(false);
    },
    onError: (error) => notifyError(null, nodeMutationMessage(error, "profile change failed")),
  });

  const current = node.tuning_profile_id;
  const fallback = profiles.data?.find((profile) => profile.is_default)?.name;
  const label = current === null ? (fallback ? `${GLOBAL_DEFAULT} · ${fallback}` : GLOBAL_DEFAULT) : profileName(profiles.data, current);
  const value = picked ?? profileValue(current);
  const locked = active || change.isPending;

  function toggle() {
    setPicked(null);
    setEditing(!editing);
  }

  function save() {
    if (!locked) change.mutate(profileFromValue(value));
  }

  return (
    <section aria-label="Tuning profile" className="glass flex flex-wrap items-center gap-2.5 p-3.5">
      <div className="min-w-0 flex-1">
        <h3 className="text-[10.5px] font-semibold uppercase tracking-[.07em] text-t3">Tuning profile</h3>
        {editing ? (
          <ProfileSelect
            aria-label="Tuning profile"
            autoFocus
            profiles={profiles.data}
            value={value}
            disabled={locked}
            onChange={(event) => setPicked(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== "Enter") return;
              event.preventDefault();
              save();
            }}
            className="mt-1 w-full"
          />
        ) : (
          <p className="mt-0.5 truncate text-[13px] font-semibold text-t1">{label}</p>
        )}
        {editing ? <ReadError query={profiles} message="Profiles did not load — only the global default can be picked" className="mt-1" /> : null}
      </div>
      <div className="flex flex-col items-end">
        <div className="flex gap-1.5">
          <Button size="sm" disabled={change.isPending || (active && !editing)} onClick={toggle}>{editing ? "Cancel" : "Change"}</Button>
          {editing ? <Button size="sm" variant="primary" disabled={locked} onClick={save}>{change.isPending ? "Saving…" : "Save"}</Button> : null}
        </div>
        {active ? <span className="mt-0.5 text-[11px] text-t3">{DISCONNECT_FIRST}</span> : null}
      </div>
      {PROFILE_SCOPE[node.protocol] ? (
        <p className="w-full text-[11px] leading-relaxed text-t3">{PROFILE_SCOPE[node.protocol]}</p>
      ) : null}
    </section>
  );
}
