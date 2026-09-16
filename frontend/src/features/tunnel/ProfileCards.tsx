import { memo } from "react";
import type { TuningProfile } from "../../api/client";
import { cn } from "../../lib/cn";
import { NO_MIMICRY } from "./profileForm";
import { ActiveBadge, DefaultBadge, FEATURES, OnOff, usedBy } from "./ProfileParts";
import { ProfileCardActions, type ProfileActionState } from "./ProfileRowActions";
import type { ProfileListProps } from "./ProfilesTable";
import type { ProfileRowCallbacks } from "./useProfileActions";

interface CardProps extends ProfileActionState {
  profile: TuningProfile;
  actions: ProfileRowCallbacks;
}

const ProfileCard = memo(function ProfileCard({ profile, actions, busy, connectionBusy, hasActiveNode }: CardProps) {
  return (
    <li data-profile-id={profile.id} aria-label={profile.name} className={cn("glass flex flex-col gap-2.5 p-3.5", profile.is_active && "shadow-[0_0_0_2px_var(--ok)]")}>
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <p className="flex min-w-0 flex-wrap items-center gap-1.5">
            <span className="truncate text-sm font-semibold text-t1">{profile.name}</span>
            {profile.is_active ? <ActiveBadge /> : null}
            {profile.is_default ? <DefaultBadge /> : null}
          </p>
          <p className="mt-0.5 text-[11.5px] text-t3">used by {usedBy(profile)} · {profile.fingerprint || NO_MIMICRY}</p>
        </div>
        <ProfileCardActions profile={profile} actions={actions} busy={busy} connectionBusy={connectionBusy} hasActiveNode={hasActiveNode} />
      </div>
      <div className="flex flex-wrap items-center gap-x-3.5 gap-y-1 text-[11px] text-t2">
        {FEATURES.map(([label, field]) => (
          <span key={field} className="inline-flex items-center gap-1.5"><OnOff on={profile[field]} label={label} /><span aria-hidden>{label}</span></span>
        ))}
        <span>QUIC <b className="font-mono font-semibold text-t1">{profile.quic}</b></span>
      </div>
    </li>
  );
});

/** T1 on a phone. */
export function ProfileCards({ profiles, actions, busy, connectionBusy, hasActiveNode }: Omit<ProfileListProps, "editingId">) {
  return (
    <ul aria-label="Profiles" className="flex flex-col gap-2.5">
      {profiles.map((profile) => (
        <ProfileCard key={profile.id} profile={profile} actions={actions} busy={busy} connectionBusy={connectionBusy} hasActiveNode={hasActiveNode} />
      ))}
    </ul>
  );
}
