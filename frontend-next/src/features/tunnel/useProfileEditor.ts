import { zodResolver } from "@hookform/resolvers/zod";
import { useCallback, useState } from "react";
import { useForm, type UseFormReturn } from "react-hook-form";
import type { TuningProfile } from "../../api/client";
import { useUnsavedGuard } from "../../app/guard";
import { confirm } from "../../components/confirm";
import { DISCARD_PROFILE_CONFIRM, blankProfileForm, cloneProfileForm, profileFormSchema, profileToForm, type ProfileFormValues } from "./profileForm";

/** What the editor holds: the profile it saves to, "new" for Create (blank, a clone or a preset), or nothing (phone list). */
export type Editing = number | "new" | null;

export interface ProfileEditorState {
  form: UseFormReturn<ProfileFormValues>;
  editing: Editing;
  dirty: boolean;
  /** Increases whenever the editor loads other values, so parts keyed by it start over (a phone's open sections). */
  generation: number;
  /** Ask before discarding unsaved edits; true when there are none or the operator agreed. */
  confirmDiscard: () => Promise<boolean>;
  /** T2 Edit: the profile's values, saved back to it. Asks first when the editor is dirty. */
  edit: (profile: TuningProfile) => Promise<void>;
  /** T2 Clone: the profile's values under "‹name› copy", saved as a new profile. Asks first when the editor is dirty. */
  clone: (profile: TuningProfile) => Promise<void>;
  /** T5 New, and after a save or a delete: a blank new profile, without asking. `open` keeps a phone's editor showing. */
  reset: (open: boolean) => void;
  /** Phone: back to the list, asking first when the editor is dirty. */
  close: () => Promise<void>;
  /** T4: put merged values in the editor, still saving to the same profile and still compared with what it loaded. */
  stage: (values: ProfileFormValues) => void;
}

/**
 * The anti-DPI editor's state for the whole screen: one react-hook-form instance, which profile it saves to, and the
 * dirty guard shared by Edit, Clone, a preset, going back to the list and leaving the screen.
 */
export function useProfileEditor(): ProfileEditorState {
  const form = useForm<ProfileFormValues>({ resolver: zodResolver(profileFormSchema), defaultValues: blankProfileForm(), mode: "onChange" });
  const dirty = form.formState.isDirty;
  useUnsavedGuard(dirty);
  const [editing, setEditing] = useState<Editing>(null);
  const [generation, setGeneration] = useState(0);
  const { reset: resetForm } = form;

  const confirmDiscard = useCallback(
    async () => !dirty || confirm(DISCARD_PROFILE_CONFIRM, { confirmLabel: "Discard" }),
    [dirty],
  );
  const load = useCallback((values: ProfileFormValues, next: Editing) => {
    resetForm(values);
    setEditing(next);
    setGeneration((value) => value + 1);
  }, [resetForm]);
  const edit = useCallback(async (profile: TuningProfile) => {
    if (await confirmDiscard()) load(profileToForm(profile), profile.id);
  }, [confirmDiscard, load]);
  const clone = useCallback(async (profile: TuningProfile) => {
    if (await confirmDiscard()) load(cloneProfileForm(profile), "new");
  }, [confirmDiscard, load]);
  const reset = useCallback((open: boolean) => load(blankProfileForm(), open ? "new" : null), [load]);
  const close = useCallback(async () => {
    if (await confirmDiscard()) load(blankProfileForm(), null);
  }, [confirmDiscard, load]);

  const stage = useCallback((values: ProfileFormValues) => {
    resetForm(values, { keepDefaultValues: true });
    setGeneration((value) => value + 1);
  }, [resetForm]);

  return { form, editing, dirty, generation, confirmDiscard, edit, clone, reset, close, stage };
}
