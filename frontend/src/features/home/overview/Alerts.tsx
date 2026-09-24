import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { Status, Subscription } from "../../../api/client";
import { serverNow } from "../../../api/clock";
import { CONNECTION_WRITE, useApiWrite, useConnectionBusy } from "../../../api/invalidation";
import { keys } from "../../../api/keys";
import { AlertBanner } from "../../../components/data/AlertBanner";
import { Chip } from "../../../components/data/Chip";
import { notifyError, notifyOk } from "../../../components/ui/Toaster";
import { cn } from "../../../lib/cn";
import { agoLabel, subWarnings } from "../../../lib/dashboard";
import { failoverBanner, hasConfigDrift, readFailoverDismissed, writeFailoverDismissed } from "../derive";

export interface AlertsProps {
  status: Status | undefined;
  subs: Subscription[] | undefined;
  /** The subscriptions read failed with nothing to show: their warnings are unknown, which is said, with Retry. */
  subsFailed?: boolean;
  onRetrySubs?: () => void;
  /** activeNodeLabel(): the node the gateway failed over to. */
  activeLabel: string;
  className?: string;
}

/** O1 config drift, O2 auto-failover, O3 subscriptions. Nothing renders when all is well. */
export function Alerts({ status, subs, subsFailed = false, onRetrySubs, activeLabel, className }: AlertsProps) {
  const [dismissed, setDismissed] = useState<number | null>(readFailoverDismissed);
  const queryClient = useQueryClient();
  const apply = useApiWrite("apply");
  const connectionBusy = useConnectionBusy();
  const reload = useMutation({
    mutationKey: CONNECTION_WRITE,
    mutationFn: (nodeId: number) => apply(nodeId),
    onSuccess: () => notifyOk("Config reloaded"),
    onError: (error) => notifyError(error, "reload failed"),
  });

  const nowMs = serverNow();
  const activeId = status?.active_node_id ?? null;
  const drift = hasConfigDrift(status) && activeId !== null;
  const failoverAt = failoverBanner(status?.last_failover_at, dismissed, nowMs);
  const warnings = subWarnings(subs ?? [], nowMs / 1000);

  if (!drift && failoverAt === null && warnings.length === 0 && !subsFailed) return null;

  // Re-apply the node that is active when the button is pressed, not the one this render saw.
  function reloadActive() {
    const nodeId = queryClient.getQueryData<Status>(keys.status)?.active_node_id ?? null;
    if (nodeId !== null) reload.mutate(nodeId);
  }

  return (
    <div className={cn("grid grid-cols-1 gap-2.5 md:grid-cols-3", className)}>
      {drift ? (
        <AlertBanner
          tone="bad"
          title="Config drift"
          text="· xray is running a different config than the one on disk"
          className="md:col-span-3"
          action={{ label: "Reload config", busyLabel: "Reloading…", busy: reload.isPending, disabled: connectionBusy, onClick: reloadActive }}
        />
      ) : null}
      {failoverAt !== null ? (
        <AlertBanner
          tone="warn"
          icon="⇄"
          title="Auto-failover"
          text={`to ${activeLabel} · ${agoLabel(failoverAt, nowMs / 1000)}`}
          onDismiss={() => {
            setDismissed(failoverAt);
            writeFailoverDismissed(failoverAt);
          }}
        />
      ) : null}
      {subsFailed ? (
        <AlertBanner
          tone="warn"
          icon="◷"
          title="Subscriptions"
          text="· did not load — expiry and data-cap warnings are unknown"
          action={onRetrySubs ? { label: "Retry", onClick: onRetrySubs } : undefined}
        />
      ) : null}
      {warnings.length > 0 ? (
        <AlertBanner tone={warnings.some((w) => w.level === "bad") ? "bad" : "warn"} icon="◷" title="Subscriptions">
          <div className="mt-1 flex flex-wrap gap-1.5">
            {warnings.map((w) => (
              <Chip key={`${w.name}-${w.text}`} tone={w.level} plain title={`Subscription ${w.name}`}>{w.name}: {w.text}</Chip>
            ))}
          </div>
        </AlertBanner>
      ) : null}
    </div>
  );
}
