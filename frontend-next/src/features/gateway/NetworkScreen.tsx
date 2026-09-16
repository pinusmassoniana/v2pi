import { useWatch } from "react-hook-form";
import type { Network as NetworkRead } from "../../api/client";
import { GATEWAY_NETWORK_POLL_MS } from "../../api/cadence";
import { NETWORK_WRITE, useConnectionBusy, useWriting } from "../../api/invalidation";
import { queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { CardHeader } from "../../components/data/CardHeader";
import { staleNotice } from "../../components/data/CardState";
import { Chip } from "../../components/data/Chip";
import { Elapsed } from "../../components/data/Elapsed";
import { Button } from "../../components/ui/Button";
import { GlassCard } from "../../components/ui/GlassCard";
import { ErrorState, Skeleton } from "../../components/ui/States";
import { GatewayDnsBody, useGatewayDns } from "./GatewayDnsCard";
import { ChecklistCard, KillSwitchState, LeasesCard, NetworkAlerts } from "./NetworkCards";
import { KillSwitchToggle, LanIpv6Fields, SegmentFields } from "./NetworkFields";
import { poolIssue } from "./networkForm";
import { useNetworkApply, type NetworkApply } from "./useNetworkApply";
import { useNetworkForm, type NetworkFormState } from "./useNetworkForm";

export const GATEWAY_CHANGED = "Changed on the gateway since you started editing — Discard to load it";

function UnsavedChip({ dirty }: { dirty: boolean }) {
  return dirty ? <span className="text-[11px] font-semibold text-warn">● unsaved changes</span> : null;
}

/** Apply is possible: something changed, nothing is invalid, the pool fits, and no connection write runs. */
function useCanApply(state: NetworkFormState, apply: NetworkApply): boolean {
  const { control, formState: { errors } } = state.form;
  const [ip, dhcpStart, dhcpEnd] = useWatch({ control, name: ["ip", "dhcpStart", "dhcpEnd"] });
  const connectionBusy = useConnectionBusy();
  return state.dirty && Object.keys(errors).length === 0 && poolIssue({ ip, dhcpStart, dhcpEnd }) === null && !connectionBusy && !apply.applying;
}

export function ApplyButton({ apply, enabled }: { apply: NetworkApply; enabled: boolean }) {
  return (
    <Button size="sm" variant="primary" disabled={!enabled} onClick={() => void apply.apply()}>
      {apply.applying ? "Applying…" : "Apply to host"}
    </Button>
  );
}

/**
 * Where Apply reports: one polite status line, mounted before it has anything to say — the pool that blocks Apply, a
 * refusal that belongs to no field, or an Apply in progress with its bar and clock.
 */
export function ApplyStatus({ state, apply }: { state: NetworkFormState; apply: NetworkApply }) {
  const [ip, dhcpStart, dhcpEnd] = useWatch({ control: state.form.control, name: ["ip", "dhcpStart", "dhcpEnd"] });
  const pool = poolIssue({ ip, dhcpStart, dhcpEnd });
  return (
    <div role="status" className="flex min-w-0 flex-1 items-center gap-2 text-[11.5px]">
      {apply.applying ? (
        <>
          <span role="progressbar" aria-label="Applying to host" className="relative h-1 min-w-16 flex-1 overflow-hidden rounded-full bg-glass-2">
            <span className="absolute inset-y-0 w-1/3 animate-pulse rounded-full bg-brand" />
          </span>
          {apply.startedAt !== null ? <span className="font-mono text-t2"><Elapsed since={apply.startedAt} format="clock" /></span> : null}
        </>
      ) : pool ? (
        <>
          <b className="font-semibold text-bad">✕ DHCP pool invalid</b>
          <span className="text-t3">fix it to apply</span>
        </>
      ) : apply.formError ? (
        <span className="whitespace-pre-wrap text-bad">{apply.formError}</span>
      ) : null}
    </div>
  );
}

/** The editor over one network read: the segment, LAN / IPv6 and kill-switch cards, leases and the router checklist. */
function NetworkEditor({ network, writing }: { network: NetworkRead; writing: boolean }) {
  const state = useNetworkForm(network);
  const { dirty, gatewayChanged, discard, form } = state;
  const killSwitch = useWatch({ control: form.control, name: "killSwitch" });
  const apply = useNetworkApply(network, state);
  const canApply = useCanApply(state, apply);
  const dns = useGatewayDns();
  // While an Apply runs, what it sends cannot be edited.
  const locked = writing || apply.applying;

  return (
    <>
      <p role="status" className={gatewayChanged ? "glass border-warn/40 p-3 text-sm text-warn" : "sr-only"}>{gatewayChanged ? GATEWAY_CHANGED : null}</p>
      <div className="grid items-start gap-3 md:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
        <div className="flex min-w-0 flex-col gap-3">
          <GlassCard aria-label="Gateway Segment" className="flex flex-col gap-3">
            <CardHeader title="Gateway Segment" detail={<Chip>nftables tproxy + policy routing</Chip>} aside={<UnsavedChip dirty={dirty} />} className="mb-0" />
            <SegmentFields state={state} disabled={locked} />
            <div className="flex flex-wrap items-center justify-end gap-2 border-t border-line pt-3">
              <ApplyStatus state={state} apply={apply} />
              <Button size="sm" disabled={!dirty || locked} onClick={discard}>Discard</Button>
              <ApplyButton apply={apply} enabled={canApply} />
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
            {dirty ? <div className="flex justify-end"><ApplyButton apply={apply} enabled={canApply} /></div> : null}
          </GlassCard>
          <GlassCard aria-label="Gateway DNS" className="flex flex-col gap-3">
            <CardHeader title="Gateway DNS" aside={<Chip>applies live when a node is connected</Chip>} className="mb-0" />
            <GatewayDnsBody dns={dns} network={network} />
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
