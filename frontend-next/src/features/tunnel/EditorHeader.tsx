import { ChevronLeft, Plus } from "lucide-react";
import type { TuningProfile } from "../../api/client";
import { Button } from "../../components/ui/Button";
import { ActiveBadge } from "./ProfileParts";
import type { Editing } from "./useProfileEditor";

export interface EditorHeaderProps {
  editing: Editing;
  /** The profile being edited, once the list has it. */
  profile: TuningProfile | undefined;
  dirty: boolean;
  onNew: () => void;
  /** Phone: back to the list. */
  onBack?: () => void;
}

/** The editor's title: which profile it saves to, its live state and use, unsaved edits, and New while editing one. */
export function EditorHeader({ editing, profile, dirty, onNew, onBack }: EditorHeaderProps) {
  const existing = typeof editing === "number";
  return (
    <div className="flex flex-wrap items-center gap-2">
      {onBack ? (
        <Button size="icon" variant="ghost" className="-ml-1.5" aria-label="Back to profiles" onClick={onBack}>
          <ChevronLeft size={18} aria-hidden />
        </Button>
      ) : null}
      <div className="min-w-0 flex-1 basis-52">
        <h2 className="text-[15px] font-bold text-t1">{existing ? `Editing profile · id ${editing}` : "New profile"}</h2>
        {existing && profile ? (
          <p className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[11.5px] text-t3">
            <span className="font-semibold text-t2">{profile.name}</span>
            {profile.is_active ? <ActiveBadge /> : null}
            <span>used by {profile.node_count} node{profile.node_count === 1 ? "" : "s"}</span>
          </p>
        ) : null}
      </div>
      {dirty ? <span className="rounded-full bg-warn/12 px-2 py-0.5 text-[11px] font-semibold text-warn">● unsaved changes</span> : null}
      {existing ? (
        <Button size="sm" onClick={onNew}><Plus size={14} aria-hidden />New</Button>
      ) : null}
    </div>
  );
}
