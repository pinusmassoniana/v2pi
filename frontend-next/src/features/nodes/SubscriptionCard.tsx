import { useMutation } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Pause, Pencil, Play, RefreshCw, Trash2 } from "lucide-react";
import { memo, useState } from "react";
import type { Subscription } from "../../api/client";
import { useApiWrite } from "../../api/invalidation";
import { confirm } from "../../components/confirm";
import { Chip } from "../../components/data/Chip";
import { Button } from "../../components/ui/Button";
import { notifyError, notifyOk } from "../../components/ui/Toaster";
import { cn } from "../../lib/cn";
import { CheckedAgo } from "./probe";
import { deleteSubMessage, intervalText, quotaFraction, quotaText, refreshOneMessage } from "./subForm";

export interface SubscriptionCardProps {
  sub: Subscription;
  /** Opens the edit form; the button shows only when given. */
  onEdit?: (sub: Subscription) => void;
}

/** U1 row with U2 refresh, U4 pause / resume and U6 delete. */
export const SubscriptionCard = memo(function SubscriptionCard({ sub, onEdit }: SubscriptionCardProps) {
  const refreshWrite = useApiWrite("refreshSub");
  const updateWrite = useApiWrite("updateSub");
  const deleteWrite = useApiWrite("deleteSub");
  const [urlOpen, setUrlOpen] = useState(false);
  const [errorOpen, setErrorOpen] = useState(false);

  const refresh = useMutation({
    mutationFn: () => refreshWrite(sub.id),
    onSuccess: (result) => {
      const outcome = refreshOneMessage(sub.name, result);
      if (outcome.error) notifyError(null, outcome.text);
      else notifyOk(outcome.text);
    },
    onError: (error) => notifyError(error, `refresh of ${sub.name} failed`),
  });
  const pause = useMutation({
    mutationFn: (enabled: boolean) => updateWrite(sub.id, { enabled }),
    onSuccess: (_saved, enabled) => notifyOk(enabled ? `Resumed ${sub.name}` : `Paused ${sub.name}`),
    onError: (error) => notifyError(error, "pause failed"),
  });
  const remove = useMutation({
    mutationFn: () => deleteWrite(sub.id),
    onSuccess: () => notifyOk(`Deleted ${sub.name}`),
    onError: (error) => notifyError(error, "delete failed"),
  });

  const fraction = quotaFraction(sub);
  const quota = quotaText(sub);

  return (
    <section
      aria-label={sub.name}
      data-sub-id={sub.id}
      data-paused={!sub.enabled || undefined}
      className={cn("glass flex flex-col gap-2.5 p-3.5 transition-opacity duration-150", !sub.enabled && "opacity-60")}
    >
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="truncate text-[15px] font-bold tracking-tight text-t1">{sub.name}</h2>
        <span className="font-mono text-[11px] text-t3">#{sub.id}</span>
        {!sub.enabled ? <Chip plain tone="neutral" className="text-t2">paused</Chip> : null}
        <Link to="/nodes" search={{ group: sub.id }} className="ml-auto text-xs font-semibold text-t1 hover:underline">
          {sub.node_count} nodes →
        </Link>
      </div>

      <button
        type="button"
        aria-expanded={urlOpen}
        aria-label={`URL of ${sub.name}`}
        title={sub.url}
        onClick={() => setUrlOpen(!urlOpen)}
        className={cn("text-left font-mono text-[11.5px] text-t2 hover:text-t1", urlOpen ? "break-all" : "truncate")}
      >
        {sub.url}
      </button>

      <div className="grid grid-cols-1 gap-2 text-xs md:grid-cols-3">
        <div className="min-w-0">
          <p className="text-[10.5px] font-semibold uppercase tracking-[.07em] text-t3">Data</p>
          {fraction !== null ? (
            <div
              role="meter"
              aria-label={`Data used by ${sub.name}`}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.round(fraction * 100)}
              className="mt-1 h-1.5 overflow-hidden rounded-full bg-glass-2"
            >
              <i className={cn("block h-full rounded-full", fraction >= 1 ? "bg-bad" : fraction >= 0.8 ? "bg-warn" : "bg-ok/70")} style={{ width: `${Math.round(fraction * 100)}%` }} />
            </div>
          ) : null}
          <p className="mt-1 truncate text-t2">{quota || "—"}</p>
        </div>
        <div className="min-w-0">
          <p className="text-[10.5px] font-semibold uppercase tracking-[.07em] text-t3">Auto-update</p>
          <p className="mt-1 text-t2">{intervalText(sub.interval_sec)}</p>
        </div>
        <div className="min-w-0">
          <p className="text-[10.5px] font-semibold uppercase tracking-[.07em] text-t3">Last refresh</p>
          <p className="mt-1 flex min-w-0 items-center gap-1 text-t2">
            {sub.last_error ? (
              <button
                type="button"
                aria-expanded={errorOpen}
                aria-label={`Last error of ${sub.name}`}
                title={sub.last_error}
                onClick={() => setErrorOpen(!errorOpen)}
                className="shrink-0 font-bold text-bad"
              >
                ⚠
              </button>
            ) : null}
            <span className="truncate">{sub.last_status ?? "—"}{sub.last_path ? ` (${sub.last_path})` : ""}</span>
          </p>
          {errorOpen && sub.last_error ? <p className="mt-0.5 break-words text-[11px] text-bad">{sub.last_error}</p> : null}
          <p className="text-[11px] text-t3">{sub.last_fetched ? <>fetched <CheckedAgo at={sub.last_fetched} /></> : "never fetched"}</p>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-1.5 border-t border-line pt-2.5">
        <Button size="sm" disabled={refresh.isPending} onClick={() => refresh.mutate()}>
          <RefreshCw size={14} aria-hidden className={refresh.isPending ? "animate-spin" : undefined} />
          {refresh.isPending ? "Refreshing…" : "Refresh"}
        </Button>
        <Button size="sm" disabled={pause.isPending} onClick={() => pause.mutate(!sub.enabled)}>
          {sub.enabled ? <Pause size={14} aria-hidden /> : <Play size={14} aria-hidden />}
          {sub.enabled ? "Pause" : "Resume"}
        </Button>
        {onEdit ? <Button size="sm" onClick={() => onEdit(sub)}><Pencil size={14} aria-hidden />Edit</Button> : null}
        <Button
          size="sm"
          variant="danger"
          className="ml-auto"
          disabled={remove.isPending}
          onClick={async () => {
            if (await confirm(deleteSubMessage(sub), { confirmLabel: "Delete" })) remove.mutate();
          }}
        >
          <Trash2 size={14} aria-hidden />Delete
        </Button>
      </div>
    </section>
  );
});
