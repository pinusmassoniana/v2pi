import { useWatch } from "react-hook-form";
import type { Network as NetworkRead } from "../../api/client";
import { GATEWAY_NETWORK_POLL_MS } from "../../api/cadence";
import { NETWORK_WRITE, useWriting } from "../../api/invalidation";
import { queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { CardHeader } from "../../components/data/CardHeader";
import { staleNotice } from "../../components/data/CardState";
import { Chip } from "../../components/data/Chip";
import { Button } from "../../components/ui/Button";
import { GlassCard } from "../../components/ui/GlassCard";
import { ErrorState, Skeleton } from "../../components/ui/States";
import { ChecklistCard, KillSwitchState, LeasesCard, NetworkAlerts } from "./NetworkCards";
import { KillSwitchToggle, LanIpv6Fields, SegmentFields } from "./NetworkFields";
import { useNetworkForm } from "./useNetworkForm";

export const GATEWAY_CHANGED = "Changed on the gateway since you started editing — Discard to load it";

function UnsavedChip({ dirty }: { dirty: boolean }) {
  return dirty ? <span className="text-[11px] font-semibold text-warn">● unsaved changes</span> : null;
}

/** The editor over one network read: the segment, LAN / IPv6 and kill-switch cards, leases and the router checklist. */
function NetworkEditor({ network, writing }: { network: NetworkRead; writing: boolean }) {
  const state = useNetworkForm(network);
  const { dirty, gatewayChanged, discard, form } = state;
  const killSwitch = useWatch({ control: form.control, name: "killSwitch" });
  const locked = writing;

  return (
    <>
      <p role="status" className={gatewayChanged ? "glass border-warn/40 p-3 text-sm text-warn" : "sr-only"}>{gatewayChanged ? GATEWAY_CHANGED : null}</p>
      <div className="grid items-start gap-3 md:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
        <div className="flex min-w-0 flex-col gap-3">
          <GlassCard aria-label="Gateway Segment" className="flex flex-col gap-3">
            <CardHeader title="Gateway Segment" detail={<Chip>nftables tproxy + policy routing</Chip>} aside={<UnsavedChip dirty={dirty} />} className="mb-0" />
            <SegmentFields state={state} disabled={locked} />
            <div className="flex flex-wrap items-center justify-end gap-2 border-t border-line pt-3">
              <Button size="sm" disabled={!dirty || locked} onClick={discard}>Discard</Button>
            </div>
          </GlassCard>
          <GlassCard aria-label="LAN access & IPv6" className="flex flex-col gap-3">
            <CardHeader title="LAN access & IPv6" aside={<span className="text-[11px] text-t3">staged — saved by Apply to host</span>} className="mb-0" />
            <LanIpv6Fields state={state} network={network} disabled={locked} desktop />
          </GlassCard>
        </div>
        <div className="flex min-w-0 flex-col gap-3">
          <GlassCard aria-label="Kill-switch" className="flex flex-col gap-3">
            <CardHeader title="Kill-switch" className="mb-0" />
            <KillSwitchState network={network} stagedOff={!killSwitch} />
            <KillSwitchToggle state={state} disabled={locked} applyHint />
          </GlassCard>
          <LeasesCard network={network} />
        </div>
      </div>
      <ChecklistCard network={network} />
    </>
  );
}

function NetworkSkeleton() {
  return (
    <div aria-busy className="grid gap-3 md:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
      <Skeleton className="h-96" />
      <Skeleton className="h-96" />
    </div>
  );
}

/**
 * Gateway › Network (W1–W7): the polling owner of the network read while it is mounted, every 5 s — paused while an
 * Apply runs, since the gateway's store lock would only hold the poll up.
 */
export function Network() {
  const writing = useWriting(NETWORK_WRITE);
  const network = usePolledQuery(queries.network(), writing ? 0 : GATEWAY_NETWORK_POLL_MS);
  const data = network.data;
  return (
    <div className="flex flex-col gap-3">
      {data ? <NetworkAlerts network={data} /> : null}
      {data && !writing ? staleNotice([network], "Network did not refresh") : null}
      {data ? (
        <NetworkEditor network={data} writing={writing} />
      ) : network.isError ? (
        <ErrorState message="Network did not load" onRetry={() => void network.refetch()} />
      ) : (
        <NetworkSkeleton />
      )}
    </div>
  );
}
