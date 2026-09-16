// Tuning profiles as every select and label shows them: one encoding and one name for "no profile of its own".
import type { TuningProfile } from "../api/client";

/** A profile select's value for no profile (null): the node or subscription follows the global default. */
export const NO_PROFILE = "";

/** The one label for no profile, in every select, toast and read-out. */
export const GLOBAL_DEFAULT = "(global default)";

/** The select value of a profile id. */
export function profileValue(id: number | null): string {
  return id === null ? NO_PROFILE : String(id);
}

/** The profile id of a select value. */
export function profileFromValue(value: string): number | null {
  return value === NO_PROFILE ? null : Number(value);
}

/** A profile's name; "(global default)" for none, "profile #N" while the list has not loaded or no longer has it. */
export function profileName(profiles: readonly TuningProfile[] | undefined, id: number | null): string {
  if (id === null) return GLOBAL_DEFAULT;
  return profiles?.find((profile) => profile.id === id)?.name ?? `profile #${id}`;
}
