import { ChevronDown, Download, Ellipsis, RotateCcw, ShieldCheck, Upload } from "lucide-react";
import { Button } from "../../components/ui/Button";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger, useAfterMenu,
} from "../../components/ui/DropdownMenu";
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

/** R4, R6 Reset and R7 on a phone: the header's ⋯ menu. Validate and Save sit in the sticky footer instead. */
export function RoutingMenu({ actions, onImportJson }: Pick<RoutingToolbarProps, "actions" | "onImportJson">) {
  const { after, onCloseAutoFocus } = useAfterMenu();
  const locked = actions.saving || actions.presetBusy;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button size="icon" variant="ghost" aria-label="More routing actions">
          <Ellipsis size={18} aria-hidden />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent onCloseAutoFocus={onCloseAutoFocus} className="min-w-72">
        <p className="px-2.5 pb-1 pt-1.5 text-[10.5px] font-semibold uppercase tracking-[.07em] text-t3">Import preset</p>
        {actions.presets?.map((preset) => (
          <DropdownMenuItem key={preset.name} disabled={locked} hint={preset.title} onSelect={() => after(() => void actions.stagePreset(preset.name))}>
            {preset.name}
          </DropdownMenuItem>
        ))}
        <DropdownMenuSeparator />
        {/* Copying needs the tap itself, so Export runs at once; the others open a sheet or a question after the menu closes. */}
        <DropdownMenuItem onSelect={() => void actions.exportRules()}><Download size={14} aria-hidden />Export JSON</DropdownMenuItem>
        <DropdownMenuItem disabled={locked} onSelect={() => after(onImportJson)}><Upload size={14} aria-hidden />Import JSON</DropdownMenuItem>
        <DropdownMenuItem disabled={locked} onSelect={() => after(() => void actions.reset())}><RotateCcw size={14} aria-hidden />Reset</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
