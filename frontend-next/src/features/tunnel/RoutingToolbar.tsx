import { ChevronDown, Download, RotateCcw, ShieldCheck, Upload } from "lucide-react";
import { Button } from "../../components/ui/Button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger, useAfterMenu } from "../../components/ui/DropdownMenu";
import type { RoutingActions } from "./useRoutingActions";

export interface RoutingToolbarProps {
  actions: RoutingActions;
  staged: boolean;
  /** Save stays off while another connection write runs. */
  connectionBusy: boolean;
  onImportJson: () => void;
}

/** R4, R6, R7 on a desktop: presets, JSON export / import, Reset, then Validate and Save. */
export function RoutingToolbar({ actions, staged, connectionBusy, onImportJson }: RoutingToolbarProps) {
  // A preset may ask first; let the menu close and hand focus back before the question opens.
  const { after, onCloseAutoFocus } = useAfterMenu();
  const locked = actions.saving || actions.presetBusy;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button size="sm" disabled={locked || !actions.presets}>
            Import preset <ChevronDown size={14} aria-hidden className="text-t3" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent onCloseAutoFocus={onCloseAutoFocus} className="min-w-72">
          {actions.presets?.map((preset) => (
            <DropdownMenuItem key={preset.name} hint={preset.title} onSelect={() => after(() => void actions.stagePreset(preset.name))}>
              {preset.name}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
      <Button size="sm" variant="ghost" onClick={() => void actions.exportRules()}><Download size={14} aria-hidden />Export JSON</Button>
      <Button size="sm" variant="ghost" disabled={locked} onClick={onImportJson}><Upload size={14} aria-hidden />Import JSON</Button>
      <Button size="sm" variant="ghost" disabled={locked} onClick={() => void actions.reset()}><RotateCcw size={14} aria-hidden />Reset</Button>
      <span aria-hidden className="mx-1 h-6 w-px bg-line" />
      <Button size="sm" disabled={actions.validating} onClick={() => void actions.validate()}>
        <ShieldCheck size={14} aria-hidden />{actions.validating ? "Validating…" : "Validate"}
      </Button>
      <Button size="sm" variant="primary" disabled={!staged || actions.saving || connectionBusy} onClick={() => void actions.save()}>
        {actions.saving ? "Saving…" : "Save"}
      </Button>
    </div>
  );
}
