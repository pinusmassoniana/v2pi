import { useMutation } from "@tanstack/react-query";
import { Plus, RefreshCw } from "lucide-react";
import { useState } from "react";
import type { Subscription } from "../../api/client";
import { SLOW_POLL_MS } from "../../api/cadence";
import { useApiWrite } from "../../api/invalidation";
import { queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { cardFallback, staleNotice } from "../../components/data/CardState";
import { Button } from "../../components/ui/Button";
import { EmptyState } from "../../components/ui/States";
import { notifyError, notifyOk } from "../../components/ui/Toaster";
import { RefreshAllPanel } from "./RefreshAllPanel";
import { SubscriptionCard } from "./SubscriptionCard";
import { SubscriptionFormSheet } from "./SubscriptionFormSheet";
import { refreshAllMessage, type Outcome } from "./subForm";
import { SubsSettingsCard } from "./SubsSettingsCard";

/** Nodes › Subscriptions (U1–U9, G1 subscription half): the polling owner of subscriptions while it is mounted. */
export function Subscriptions() {
  const subs = usePolledQuery(queries.subs(), SLOW_POLL_MS);
  const refreshAllWrite = useApiWrite("refreshAllSubs");
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  // The open form: "add", a subscription to edit, or none.
  const [form, setForm] = useState<"add" | Subscription | null>(null);
  const refreshAll = useMutation({
    mutationFn: () => refreshAllWrite(),
    onSuccess: (result) => {
      const next = refreshAllMessage(result);
      setOutcome(next);
      if (next.error) notifyError(null, next.text);
      else notifyOk(next.text);
    },
    onError: (error) => notifyError(error, "refresh all failed"),
  });

  const list = subs.data;
  const fallback = cardFallback([subs], "Subscriptions did not load", "h-40");
  const anyEnabled = list?.some((sub) => sub.enabled) ?? false;

  return (
    <div className="flex flex-col gap-3">
      <SubsSettingsCard />
      <div className="flex flex-wrap items-center gap-2.5 px-1">
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold text-t1">{list ? `${list.length} subscription${list.length === 1 ? "" : "s"}` : "Subscriptions"}</p>
          <p className="text-[11px] text-t3">Refresh all skips paused subscriptions; a manual Refresh still works.</p>
        </div>
        <Button size="sm" disabled={!anyEnabled || refreshAll.isPending} onClick={() => refreshAll.mutate()}>
          <RefreshCw size={14} aria-hidden className={refreshAll.isPending ? "animate-spin" : undefined} />
          {refreshAll.isPending ? "Refreshing…" : "Refresh all"}
        </Button>
        <Button size="sm" variant="primary" onClick={() => setForm("add")}>
          <Plus size={14} aria-hidden />Add subscription
        </Button>
      </div>
      {outcome ? <RefreshAllPanel outcome={outcome} onDismiss={() => setOutcome(null)} /> : null}
      {list ? staleNotice([subs], "Subscriptions did not refresh") : null}
      {fallback ?? (list && list.length === 0 ? (
        <EmptyState title="No subscriptions yet — add one" />
      ) : (
        <div className="flex flex-col gap-3">
          {list?.map((sub) => <SubscriptionCard key={sub.id} sub={sub} onEdit={setForm} />)}
        </div>
      ))}
      {form !== null ? (
        <SubscriptionFormSheet key={form === "add" ? "add" : form.id} sub={form === "add" ? undefined : form} onClose={() => setForm(null)} />
      ) : null}
    </div>
  );
}
