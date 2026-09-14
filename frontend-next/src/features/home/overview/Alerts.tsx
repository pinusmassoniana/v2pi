import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import type { Status, Subscription } from "../../../api/client";
import { serverNow } from "../../../api/clock";
import { useApiWrite } from "../../../api/invalidation";
import { useTraffic } from "../../../api/traffic";
import { AlertBanner } from "../../../components/data/AlertBanner";
import { Chip } from "../../../components/data/Chip";
import { notifyError, notifyOk } from "../../../components/ui/Toaster";
import { cn } from "../../../lib/cn";
import { agoLabel, subWarnings } from "../../../lib/dashboard";
import { fmtRate } from "../../../lib/format";
import {
  UNTUNNELED_ALERT_BPS, bypassState, failoverBanner, hasConfigDrift, readFailoverDismissed, writeFailoverDismissed,
} from "../derive";

export interface AlertsProps {
  status: Status | undefined;
  subs: Subscription[] | undefined;
  activeName: string | null;
  className?: string;
}

/** O1 config drift, O2 auto-failover, O3 subscriptions, O5 untunneled traffic. Nothing renders when all is well. */
export function Alerts({ status, subs, activeName, className }: AlertsProps) {
  const traffic = useTraffic();
  const [dismissed, setDismissed] = useState<number | null>(readFailoverDismissed);
  const apply = useApiWrite("apply");
  const reload = useMutation({
    mutationFn: (nodeId: number) => apply(nodeId),
    onSuccess: () => notifyOk("Config reloaded"),
    onError: (error) => notifyError(error, "reload failed"),
  });

  const nowMs = serverNow();
  const activeId = status?.active_node_id ?? null;
  const drift = hasConfigDrift(status) && activeId !== null;
  const failoverAt = failoverBanner(status?.last_failover_at, dismissed, nowMs);
  const warnings = subWarnings(subs ?? [], nowMs / 1000);
  const bypass = bypassState(traffic.disabled ? null : traffic.live);

  if (!drift && failoverAt === null && warnings.length === 0 && !bypass.alert) return null;

  return (
    <div className={cn("grid grid-cols-1 gap-2.5 md:grid-cols-3", className)}>
      {drift ? (
        <AlertBanner
          tone="bad"
          title="Config drift"
          text="· xray is running a different config than the one on disk"
          className="md:col-span-3"
          action={{ label: "Reload config", busyLabel: "Reloading…", busy: reload.isPending, onClick: () => reload.mutate(activeId) }}
        />
      ) : null}
      {failoverAt !== null ? (
        <AlertBanner
          tone="warn"
          icon="⇄"
          title="Auto-failover"
          text={`to ${activeName ?? "another node"} · ${agoLabel(failoverAt, nowMs / 1000)}`}
          onDismiss={() => {
            setDismissed(failoverAt);
            writeFailoverDismissed(failoverAt);
          }}
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
      {bypass.alert ? (
        <AlertBanner tone="warn" icon="↯" title="Traffic bypassing the tunnel" text={`· ${fmtRate(bypass.total)}`}>
          <p className="text-[11px] text-t2">threshold {fmtRate(UNTUNNELED_ALERT_BPS)}</p>
        </AlertBanner>
      ) : null}
    </div>
  );
}
