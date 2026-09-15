import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useCallback } from "react";
import { SLOW_POLL_MS } from "../../api/cadence";
import { useConnectionBusy, useProfileBusy } from "../../api/invalidation";
import { queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { staleNotice } from "../../components/data/CardState";
import { Button } from "../../components/ui/Button";
import { GlassCard } from "../../components/ui/GlassCard";
import { EmptyState, ErrorState, Skeleton } from "../../components/ui/States";
import { DESKTOP_QUERY, useMediaQuery } from "../../lib/media";
import { EditorHeader } from "./EditorHeader";
import { ProfileCards } from "./ProfileCards";
import { HEADER_HINT } from "./profileForm";
import { NO_ACTIVE_NODE_HINT } from "./ProfileRowActions";
import { ProfilesTable } from "./ProfilesTable";
import { useProfileActions } from "./useProfileActions";
import { useProfileEditor } from "./useProfileEditor";

/**
 * Tunnel › Anti-DPI (T1–T5): the polling owner of the profiles while it is mounted. On a desktop the list and the
 * editor side by side; on a phone the list, or the editor as a full page.
 */
export function AntiDpi() {
  const profiles = usePolledQuery(queries.profiles(), SLOW_POLL_MS);
  const activeNodeId = useQuery({ ...queries.status(), select: (status) => status.active_node_id });
  const desktop = useMediaQuery(DESKTOP_QUERY);
  const busy = useProfileBusy();
  const connectionBusy = useConnectionBusy();
  const editor = useProfileEditor();
  const { editing, reset } = editor;
  const onDeleted = useCallback((id: number) => {
    if (editing === id) reset(false);
  }, [editing, reset]);
  const actions = useProfileActions({ edit: editor.edit, clone: editor.clone, onDeleted });

  const list = profiles.data;
  const hasActiveNode = activeNodeId.data !== null && activeNodeId.data !== undefined;
  const editingId = typeof editing === "number" ? editing : null;
  const showEditor = desktop || editing !== null;
  const showList = desktop || editing === null;

  return (
    <div className="grid items-start gap-3 min-[1180px]:grid-cols-[minmax(0,1fr)_minmax(0,30rem)]">
      {showList ? (
        <section aria-label="Anti-DPI profiles" className="flex min-w-0 flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2 px-1">
            <h2 className="text-[15px] font-bold text-t1">{list ? `${list.length} profile${list.length === 1 ? "" : "s"}` : "Profiles"}</h2>
            <span className="text-xs text-t3">· ⟳ live-apply</span>
            {!desktop ? (
              <Button size="sm" variant="primary" className="ml-auto" onClick={() => editor.reset(true)}>
                <Plus size={14} aria-hidden />New profile
              </Button>
            ) : null}
          </div>
          <p className="px-1 text-xs text-t2">{HEADER_HINT}</p>
          {list && activeNodeId.data === null ? <p className="px-1 text-xs text-warn">⚡ {NO_ACTIVE_NODE_HINT}</p> : null}
          {list ? staleNotice([profiles], "Profiles did not refresh") : null}
          {list === undefined ? (
            profiles.isError ? (
              <ErrorState message="Profiles did not load" onRetry={() => void profiles.refetch()} />
            ) : (
              <GlassCard aria-label="Profiles" aria-busy className="flex flex-col gap-2">
                {[0, 1, 2].map((row) => <Skeleton key={row} className="h-11" />)}
              </GlassCard>
            )
          ) : list.length === 0 ? (
            <EmptyState title="No profiles" />
          ) : desktop ? (
            <GlassCard className="overflow-hidden p-0">
              <ProfilesTable profiles={list} editingId={editingId} actions={actions} busy={busy} connectionBusy={connectionBusy} hasActiveNode={hasActiveNode} />
            </GlassCard>
          ) : (
            <ProfileCards profiles={list} actions={actions} busy={busy} connectionBusy={connectionBusy} hasActiveNode={hasActiveNode} />
          )}
        </section>
      ) : null}
      {showEditor ? (
        <GlassCard aria-label="Profile editor" className="flex min-w-0 flex-col gap-3 min-[1180px]:sticky min-[1180px]:top-5">
          <EditorHeader
            editing={editing}
            profile={list?.find((profile) => profile.id === editingId)}
            dirty={editor.dirty}
            onNew={() => editor.reset(true)}
            onBack={desktop ? undefined : () => void editor.close()}
          />
        </GlassCard>
      ) : null}
    </div>
  );
}
