import type { ComponentProps } from "react";
import type { TuningProfile } from "../../api/client";
import { GLOBAL_DEFAULT, NO_PROFILE } from "../../lib/profiles";
import { Select } from "./Select";

/** The value of the placeholder option: not a profile, and not "no profile" either. */
export const PICK_PROFILE = "pick";

export interface ProfileSelectProps extends Omit<ComponentProps<typeof Select>, "children"> {
  /** The tuning profiles; the options fill in once they load. */
  profiles: readonly TuningProfile[] | undefined;
  /** A first, unselectable option (value PICK_PROFILE) for a select that acts on each pick. */
  placeholder?: string;
}

/** A tuning-profile select: "(global default)" (value NO_PROFILE, for null), then every profile by id. */
export function ProfileSelect({ profiles, placeholder, ...props }: ProfileSelectProps) {
  return (
    <Select {...props}>
      {placeholder ? <option value={PICK_PROFILE} disabled>{placeholder}</option> : null}
      <option value={NO_PROFILE}>{GLOBAL_DEFAULT}</option>
      {profiles?.map((profile) => <option key={profile.id} value={String(profile.id)}>{profile.name}</option>)}
    </Select>
  );
}
