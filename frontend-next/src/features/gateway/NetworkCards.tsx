import type { Network } from "../../api/client";
import { useNow } from "../../components/data/Ago";
import { AlertBanner } from "../../components/data/AlertBanner";
import { CardHeader } from "../../components/data/CardHeader";
import { Chip } from "../../components/data/Chip";
import { StepList } from "../../components/data/StepList";
import { GlassCard } from "../../components/ui/GlassCard";
import { Pill } from "../../components/ui/Pill";
import { killSwitchState } from "../../lib/network";
import { leaseKey, leaseLeft, sortLeases } from "./leases";
import { KILL_SWITCH_CAPTION, networkWarnings } from "./networkForm";

/** W1: one banner per condition the host reports, above everything else. None of them can be dismissed. */
export function NetworkAlerts({ network }: { network: Network }) {
  const warnings = networkWarnings(network);
  if (warnings.length === 0) return null;
  return (
    <div className="flex flex-col gap-2">
      {warnings.map((warning) => (
        <AlertBanner key={warning.key} tone={warning.tone} title={warning.title} text={warning.text}>
          {warning.detail ? <p aria-live="off" className="mt-0.5 font-mono text-[11.5px] text-t2">{warning.detail}</p> : null}
        </AlertBanner>
      ))}
    </div>
  );
}

/** W6: the kill-switch as the host holds it — never the unsaved toggle — with its caption and what Apply would change. */
export function KillSwitchState({ network, stagedOff }: { network: Network; stagedOff: boolean }) {
  const state = killSwitchState(network);
  const disarming = network.kill_switch_enabled && stagedOff;
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs text-t2">{KILL_SWITCH_CAPTION[state.label]}</p>
        <Pill tone={state.tone} dot>{state.label}</Pill>
      </div>
      <p role="status" className={disarming ? "self-start" : "sr-only"}>
        {disarming ? <Chip tone="warn" plain>will disarm on Apply</Chip> : null}
      </p>
    </div>
  );
}

/** W7: the leases the gateway's DHCP server handed out, in address order, with the time each has left. */
export function LeasesList({ network, cards = false }: { network: Network; cards?: boolean }) {
  const now = useNow();
  const leases = sortLeases(network.status.clients);
  if (leases.length === 0) return <p className="text-sm text-t3">No active leases.</p>;
  return (
    <ul aria-label="Leases" className={cards ? "flex flex-col gap-2" : "flex flex-col divide-y divide-line"}>
      {leases.map((lease) => (
        <li
          key={leaseKey(lease)}
          className={cards ? "glass flex items-center gap-3 bg-solid px-3 py-2.5 text-[13px]" : "flex items-center gap-3 py-2 text-[13px] first:pt-0 last:pb-0"}
        >
          <span className="w-32 shrink-0 font-mono font-semibold text-t1">{lease.ip}</span>
          <span className="min-w-0 flex-1 truncate text-t2">{lease.hostname || "—"}</span>
          <span className={lease.expiry === 0 ? "shrink-0 font-semibold text-t1" : "shrink-0 text-t3"}>{leaseLeft(lease.expiry, now)}</span>
        </li>
      ))}
    </ul>
  );
}

export function LeasesCard({ network, className }: { network: Network; className?: string }) {
  return (
    <GlassCard aria-label="DHCP leases" className={className}>
      <CardHeader title="DHCP leases" detail={`${network.status.dhcp_clients} active`} aside={<Chip tone="ok">live</Chip>} />
      <LeasesList network={network} />
    </GlassCard>
  );
}

/** W5: the router steps built from the saved plan — numbered steps, never checks: the gateway verifies none of them. */
export function Checklist({ network, collapsible = false }: { network: Network; collapsible?: boolean }) {
  if (network.recommendations.length === 0) return <p className="text-sm text-t3">No router actions outstanding.</p>;
  return (
    <StepList
      collapsible={collapsible}
      className={collapsible ? undefined : "md:grid md:grid-cols-2 md:gap-x-6"}
      steps={network.recommendations.map((rec) => ({ key: rec.title, title: rec.title, detail: rec.detail }))}
    />
  );
}

export function ChecklistCard({ network }: { network: Network }) {
  return (
    <GlassCard aria-label="Router checklist">
      <CardHeader title="Router checklist" detail="the one box v2pi never touches" aside={<span className="text-[11px] text-t3">from the saved plan · changes after Apply</span>} />
      <Checklist network={network} />
    </GlassCard>
  );
}
