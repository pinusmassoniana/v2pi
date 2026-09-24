import { useState } from "react";
import { useWatch } from "react-hook-form";
import type { Network as NetworkRead } from "../../api/client";
import { GATEWAY_NETWORK_POLL_MS } from "../../api/cadence";
import { NETWORK_WRITE, useConnectionBusy, useWriting } from "../../api/invalidation";
import { queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { CardHeader } from "../../components/data/CardHeader";
import { cardFallback, staleNotice } from "../../components/data/CardState";
import { Chip } from "../../components/data/Chip";
import { Elapsed } from "../../components/data/Elapsed";
import { Button } from "../../components/ui/Button";
import { GlassCard } from "../../components/ui/GlassCard";
import { ErrorState, Skeleton } from "../../components/ui/States";
import { DESKTOP_QUERY, useMediaQuery } from "../../lib/media";
import { EditorSection } from "../tunnel/EditorSection";
import { ClientDnsHint, GatewayDnsBody, GatewayDnsSwitch, useGatewayDns } from "./GatewayDnsCard";
import { DevicesCard, useDevices, type DevicesState } from "./DevicesCard";
import { Checklist, ChecklistCard, KillSwitchState, LeasesCard, LeasesList, NetworkAlerts } from "./NetworkCards";
import { KillSwitchToggle, LanIpv6Fields, SegmentFields } from "./NetworkFields";
import { poolIssue, type NetworkField } from "./networkForm";
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

interface EditorParts {
  network: NetworkRead;
  state: NetworkFormState;
  apply: NetworkApply;
  canApply: boolean;
  dns: ReturnType<typeof useGatewayDns>;
  devices: DevicesState;
  locked: boolean;
}

/** The editor over one network read: its form, Apply and the gateway DNS switch, laid out for a desktop or a phone. */
function NetworkEditor({ network, writing }: { network: NetworkRead; writing: boolean }) {
  const desktop = useMediaQuery(DESKTOP_QUERY);
  const state = useNetworkForm(network);
  const apply = useNetworkApply(network, state);
  const canApply = useCanApply(state, apply);
  const dns = useGatewayDns();
  const devices = useDevices();
  // While an Apply runs, what it sends cannot be edited.
  const parts: EditorParts = { network, state, apply, canApply, dns, devices, locked: writing || apply.applying };
  return (
    <>
      <p role="status" className={state.gatewayChanged ? "glass border-warn/40 p-3 text-sm text-warn" : "sr-only"}>{state.gatewayChanged ? GATEWAY_CHANGED : null}</p>
      {desktop ? <NetworkDesktop {...parts} /> : <NetworkPhone {...parts} />}
    </>
  );
}

/** Desktop: the segment and LAN / IPv6 cards on the left, kill-switch, gateway DNS and leases on the right, then the checklist. */
function NetworkDesktop({ network, state, apply, canApply, dns, devices, locked }: EditorParts) {
  const { dirty, discard, form } = state;
  const killSwitch = useWatch({ control: form.control, name: "killSwitch" });
  return (
    <>
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
          <DevicesCard network={network} devices={devices} />
          <LeasesCard network={network} />
        </div>
      </div>
      <ChecklistCard network={network} />
    </>
  );
}

type PhoneSection = "segment" | "ipv6" | "dns" | "checklist";

const FIELD_SECTION: Partial<Record<NetworkField, PhoneSection>> = {
  iface: "segment", ip: "segment", dhcpStart: "segment", dhcpEnd: "segment", clientDns: "segment", lease: "segment",
  ip6Mode: "ipv6", ip6Static: "ipv6", clientDns6: "ipv6", lanAccess: "ipv6", ipv6: "ipv6",
};

/**
 * Phone: the kill-switch and the leases first, then collapsible sections with their summaries, and a sticky footer
 * with Discard and Apply while something is edited. A section holding an error — or the segment while its pool is
 * invalid — stays open, so the reason Apply is unavailable is always on screen.
 */
function NetworkPhone({ network, state, apply, canApply, dns, devices, locked }: EditorParts) {
  const { dirty, discard, form } = state;
  const { control, formState: { errors } } = form;
  const [killSwitch, ip, dhcpStart, dhcpEnd, lanAccess, ipv6, ip6Mode] = useWatch({ control, name: ["killSwitch", "ip", "dhcpStart", "dhcpEnd", "lanAccess", "ipv6", "ip6Mode"] });
  const [toggled, setToggled] = useState<Record<PhoneSection, boolean>>({ segment: false, ipv6: false, dns: false, checklist: false });
  const failing = new Set((Object.keys(errors) as NetworkField[]).map((field) => FIELD_SECTION[field]));
  if (poolIssue({ ip, dhcpStart, dhcpEnd })) failing.add("segment");
  // A DNS read that failed opens its section too: the switch in the header can only read off and disabled, and the
  // reason (with Retry) is in the section's body.
  if (dns.settings.isError && dns.settings.data === undefined) failing.add("dns");
  const section = (name: PhoneSection) => {
    const open = toggled[name] || failing.has(name);
    return { collapsible: true, open, onToggle: () => setToggled((current) => ({ ...current, [name]: !open })) };
  };
  const mode = ip6Mode === "static" ? "static /64" : ip6Mode === "auto" ? "auto" : "ULA";
  const steps = network.recommendations.length;
  return (
    <div className="flex flex-col gap-3 [&_input]:scroll-mb-44">
      <GlassCard aria-label="Kill-switch" className="flex flex-col gap-3">
        <CardHeader title="Kill-switch" className="mb-0" />
        <KillSwitchState network={network} stagedOff={!killSwitch} />
        <KillSwitchToggle state={state} disabled={locked} applyHint={false} />
      </GlassCard>
      <DevicesCard network={network} devices={devices} />
      <section aria-label="DHCP leases" className="flex flex-col gap-2">
        <CardHeader title="DHCP leases" detail={`${network.status.dhcp_clients} active`} aside={<Chip tone="ok">live</Chip>} className="mb-0 px-1" />
        <LeasesList network={network} cards />
      </section>
      <GlassCard className="flex flex-col px-4 py-1">
        <EditorSection title="Gateway Segment" note="nftables tproxy + policy routing" aside={<UnsavedChip dirty={dirty} />} {...section("segment")}>
          <SegmentFields state={state} disabled={locked} />
        </EditorSection>
        <EditorSection
          title="LAN access & IPv6"
          note="staged — saved by Apply to host"
          summary={`LAN ${lanAccess ? "on" : "off"} · IPv6 ${ipv6 ? `on · ${mode}` : "off"}`}
          {...section("ipv6")}
        >
          <LanIpv6Fields state={state} network={network} disabled={locked} desktop={false} />
        </EditorSection>
        <EditorSection
          title="Gateway DNS"
          note="applies live when a node is connected"
          summary={dns.settings.data ? (dns.settings.data.dns_intercept ? "on" : "off") : undefined}
          aside={<GatewayDnsSwitch dns={dns} />}
          {...section("dns")}
        >
          {cardFallback([dns.settings], "Gateway DNS did not load", "h-12") ?? <ClientDnsHint network={network} desktop={false} />}
        </EditorSection>
        <EditorSection title="Router checklist" note="the one box v2pi never touches" summary={`${steps} step${steps === 1 ? "" : "s"}`} {...section("checklist")}>
          <p className="text-[11px] text-t3">from the saved plan · changes after Apply</p>
          <Checklist network={network} collapsible />
        </EditorSection>
      </GlassCard>
      <div className={dirty ? "glass sticky bottom-24 z-20 flex flex-wrap items-center gap-2 bg-solid p-2.5" : "contents"}>
        <ApplyStatus state={state} apply={apply} />
        {dirty ? (
          <div className="ml-auto flex items-center gap-2">
            <Button size="sm" disabled={locked} onClick={discard}>Discard</Button>
            <ApplyButton apply={apply} enabled={canApply} />
          </div>
        ) : null}
      </div>
    </div>
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
