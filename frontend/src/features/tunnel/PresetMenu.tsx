import { useQuery } from "@tanstack/react-query";
import { ChevronDown, Menu } from "lucide-react";
import type { ProfilePreset } from "../../api/client";
import { queries } from "../../api/keys";
import { Button } from "../../components/ui/Button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger, useAfterMenu } from "../../components/ui/DropdownMenu";
import { ReadError } from "../../components/ui/States";
import { notifyOk } from "../../components/ui/Toaster";
import { presetStagedMessage, stagePreset } from "./profileForm";
import type { ProfileEditorState } from "./useProfileEditor";

/** T4: stage a preset into the editor, after the dirty guard. `compact` is the phone footer's icon button. */
export function PresetMenu({ editor, compact = false }: { editor: ProfileEditorState; compact?: boolean }) {
  const presets = useQuery(queries.profilePresets());   // read once; nothing polls presets
  // The dirty guard may ask; let the menu close and hand focus back before the question opens.
  const { after, onCloseAutoFocus } = useAfterMenu();

  async function stage(preset: ProfilePreset) {
    if (!(await editor.confirmDiscard())) return;
    editor.stage(stagePreset(editor.form.getValues(), preset));
    notifyOk(presetStagedMessage(preset.name));
  }

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          {compact ? (
            <Button size="icon" aria-label="Stage preset" disabled={!presets.data}><Menu size={16} aria-hidden /></Button>
          ) : (
            <Button size="sm" disabled={!presets.data}>Stage preset… <ChevronDown size={14} aria-hidden className="text-t3" /></Button>
          )}
        </DropdownMenuTrigger>
        <DropdownMenuContent onCloseAutoFocus={onCloseAutoFocus} align="start" className="min-w-72">
          {presets.data?.map((preset) => (
            <DropdownMenuItem key={preset.name} hint={preset.title} onSelect={() => after(() => void stage(preset))}>{preset.name}</DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
      <ReadError query={presets} message="Presets did not load" />
    </>
  );
}
