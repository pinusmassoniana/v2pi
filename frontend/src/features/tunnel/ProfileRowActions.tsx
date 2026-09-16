import { Copy, Ellipsis, Pencil, Star, Trash2, Zap } from "lucide-react";
import type { TuningProfile } from "../../api/client";
import { Button } from "../../components/ui/Button";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger, useAfterMenu,
} from "../../components/ui/DropdownMenu";
import { cn } from "../../lib/cn";
import type { ProfileRowCallbacks } from "./useProfileActions";

export const APPLY_TITLE = "Apply to the active node and re-apply now";
export const NO_ACTIVE_NODE_HINT = "No active node — connect one to apply a profile live.";

/** What disables row actions: any profile write, a connection write (for those that move the tunnel), no active node. */
export interface ProfileActionState {
  busy: boolean;
  connectionBusy: boolean;
  hasActiveNode: boolean;
}

export interface ProfileRowActionsProps extends ProfileActionState {
  profile: TuningProfile;
  actions: ProfileRowCallbacks;
}

/** T2 on a desktop: Edit, Clone, ⚡ Apply to active, ☆ Make default and Delete; the last two keep their place on the default. */
export function ProfileRowActions({ profile, actions, busy, connectionBusy, hasActiveNode }: ProfileRowActionsProps) {
  const live = busy || connectionBusy;
  return (
    <div className="flex items-center justify-end gap-1">
      <Button size="icon" variant="ghost" className="size-8" title="Edit" aria-label={`Edit ${profile.name}`} disabled={busy} onClick={() => actions.onEdit(profile)}>
        <Pencil size={15} aria-hidden />
      </Button>
      <Button size="icon" variant="ghost" className="size-8" title="Clone" aria-label={`Clone ${profile.name}`} disabled={busy} onClick={() => actions.onClone(profile)}>
        <Copy size={15} aria-hidden />
      </Button>
      <Button
        size="icon"
        variant="ghost"
        className="size-8 text-g1 hover:text-g1"
        title={hasActiveNode ? APPLY_TITLE : NO_ACTIVE_NODE_HINT}
        aria-label={`Apply ${profile.name} to the active node`}
        disabled={live || !hasActiveNode}
        onClick={() => actions.onApply(profile)}
      >
        <Zap size={15} aria-hidden />
      </Button>
      {profile.is_default ? (
        <span aria-hidden className="size-8" />
      ) : (
        <Button size="icon" variant="ghost" className="size-8" title="Make default" aria-label={`Make ${profile.name} the default`} disabled={live} onClick={() => actions.onMakeDefault(profile)}>
          <Star size={15} aria-hidden />
        </Button>
      )}
      {profile.is_default ? (
        <span aria-hidden className="size-8" />
      ) : (
        <Button
          size="icon"
          variant="ghost"
          className="size-8 text-bad hover:text-bad"
          title="Delete"
          aria-label={`Delete ${profile.name}`}
          disabled={busy || (profile.is_active && connectionBusy)}
          onClick={() => actions.onDelete(profile)}
        >
          <Trash2 size={15} aria-hidden />
        </Button>
      )}
    </div>
  );
}

/** T2 on a phone: ⚡ Apply on the card, the rest in its ⋯ menu (no Make default or Delete on the default). */
export function ProfileCardActions({ profile, actions, busy, connectionBusy, hasActiveNode }: ProfileRowActionsProps) {
  const { after, onCloseAutoFocus } = useAfterMenu();
  const live = busy || connectionBusy;
  return (
    <div className="flex items-center gap-1.5">
      <Button
        size="sm"
        className={cn("text-g1")}
        title={hasActiveNode ? APPLY_TITLE : NO_ACTIVE_NODE_HINT}
        aria-label={`Apply ${profile.name} to the active node`}
        disabled={live || !hasActiveNode}
        onClick={() => actions.onApply(profile)}
      >
        <Zap size={14} aria-hidden />Apply
      </Button>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button size="icon" variant="ghost" className="size-9" aria-label={`More actions for ${profile.name}`}>
            <Ellipsis size={16} aria-hidden />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent onCloseAutoFocus={onCloseAutoFocus}>
          <DropdownMenuItem disabled={busy} onSelect={() => after(() => actions.onEdit(profile))}><Pencil size={14} aria-hidden />Edit</DropdownMenuItem>
          <DropdownMenuItem disabled={busy} onSelect={() => after(() => actions.onClone(profile))}><Copy size={14} aria-hidden />Clone</DropdownMenuItem>
          {profile.is_default ? null : (
            <>
              <DropdownMenuItem disabled={live} onSelect={() => after(() => actions.onMakeDefault(profile))}><Star size={14} aria-hidden />Make default</DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                disabled={busy || (profile.is_active && connectionBusy)}
                onSelect={() => after(() => actions.onDelete(profile))}
                className="text-bad"
              >
                <Trash2 size={14} aria-hidden />Delete…
              </DropdownMenuItem>
            </>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
