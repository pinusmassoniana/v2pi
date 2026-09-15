import { memo } from "react";
import type { TuningProfile } from "../../api/client";
import { cn } from "../../lib/cn";
import { NO_MIMICRY } from "./profileForm";
import { ActiveBadge, DefaultBadge, FEATURES, OnOff, usedBy } from "./ProfileParts";
import { ProfileRowActions, type ProfileActionState } from "./ProfileRowActions";
import type { ProfileRowCallbacks } from "./useProfileActions";

export interface ProfileListProps extends ProfileActionState {
  profiles: readonly TuningProfile[];
  /** The profile loaded in the editor, if any. */
  editingId: number | null;
  actions: ProfileRowCallbacks;
}

interface RowProps extends ProfileActionState {
  profile: TuningProfile;
  editing: boolean;
  actions: ProfileRowCallbacks;
}

/** One profile; memoised, so a poll that changes one profile re-renders that row only. */
const ProfileRow = memo(function ProfileRow({ profile, editing, actions, busy, connectionBusy, hasActiveNode }: RowProps) {
  return (
    <tr data-profile-id={profile.id} data-editing={editing || undefined} className={cn("border-t border-line", editing && "bg-g2/10", profile.is_active && "shadow-[inset_3px_0_0_var(--ok)]")}>
      <td className="py-2.5 pl-3 pr-2">
        <div className="flex min-w-0 flex-wrap items-center gap-1.5">
          <span className="truncate font-semibold text-t1">{profile.name}</span>
          {profile.is_active ? <ActiveBadge /> : null}
          {profile.is_default ? <DefaultBadge /> : null}
        </div>
      </td>
      <td className="px-2 py-2.5 font-mono text-xs text-t2">{usedBy(profile)}</td>
      <td className="px-2 py-2.5 text-xs text-t2">{profile.fingerprint || NO_MIMICRY}</td>
      {FEATURES.map(([label, field]) => (
        <td key={field} className="px-2 py-2.5 text-center"><OnOff on={profile[field]} label={label} /></td>
      ))}
      <td className="px-2 py-2.5 font-mono text-xs text-t2">{profile.quic}</td>
      <td className="py-2 pl-2 pr-3">
        <ProfileRowActions profile={profile} actions={actions} busy={busy} connectionBusy={connectionBusy} hasActiveNode={hasActiveNode} />
      </td>
    </tr>
  );
});

/** T1 on a desktop. */
export function ProfilesTable({ profiles, editingId, actions, busy, connectionBusy, hasActiveNode }: ProfileListProps) {
  return (
    <div className="overflow-x-auto">
      <table aria-label="Profiles" className="w-full min-w-[640px] text-left text-sm">
        <thead className="text-[10px] uppercase tracking-[.07em] text-t3">
          <tr>
            <th scope="col" className="py-2.5 pl-3 pr-2 font-semibold">Name</th>
            <th scope="col" className="px-2 font-semibold">Used by</th>
            <th scope="col" className="px-2 font-semibold">Fingerprint</th>
            {FEATURES.map(([label]) => <th key={label} scope="col" className="px-2 text-center font-semibold">{label}</th>)}
            <th scope="col" className="px-2 font-semibold">QUIC</th>
            <th scope="col" className="pl-2 pr-3 text-right font-semibold">Actions</th>
          </tr>
        </thead>
        <tbody>
          {profiles.map((profile) => (
            <ProfileRow
              key={profile.id}
              profile={profile}
              editing={profile.id === editingId}
              actions={actions}
              busy={busy}
              connectionBusy={connectionBusy}
              hasActiveNode={hasActiveNode}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}
