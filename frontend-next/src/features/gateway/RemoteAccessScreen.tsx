import { useQuery } from "@tanstack/react-query";
import { useWatch } from "react-hook-form";
import type { Rw } from "../../api/client";
import { RW_POLL_MS } from "../../api/cadence";
import { RW_WRITE, useConnectionBusy, useWriting } from "../../api/invalidation";
import { keys, queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { useRefetchOnChange } from "../../api/useRefetchOnChange";
import { AlertBanner } from "../../components/data/AlertBanner";
import { CardHeader } from "../../components/data/CardHeader";
import { staleNotice } from "../../components/data/CardState";
import { Chip } from "../../components/data/Chip";
import { Button } from "../../components/ui/Button";
import { GlassCard } from "../../components/ui/GlassCard";
import { ErrorState, Skeleton } from "../../components/ui/States";
import { HOSTS_NOTE, EnableToggle, HostRows, InboundFields, RwChecklist, SubnetFields, WithCode } from "./RwFields";
import { MAX_HOSTS, rwWarnings } from "./rwForm";
import { useRwForm, type RwFormState } from "./useRwForm";
import { useRwSave, type RwSave } from "./useRwSave";

export const RW_GATEWAY_CHANGED = "Changed on the gateway since you started editing — Discard to load it";

const RW_KEYS = [keys.rw] as const;

/** A1: the banners, in order. Revocation pending and malformed state cannot be dismissed; none of these can. */
function RwAlerts({ rw, state }: { rw: Rw; state: RwFormState }) {
  const [dest, serverNames] = useWatch({ control: state.form.control, name: ["dest", "serverNames"] });
  const ownWrite = useWriting(RW_WRITE);
  const warnings = rwWarnings(rw, { dest, serverNames }, ownWrite);
  if (warnings.length === 0) return null;
  return (
    <div className="flex flex-col gap-2">
      {warnings.map((warning) => (
        <AlertBanner key={warning.key} tone={warning.tone} title={warning.title} text={warning.text}>
          {warning.badge ? <Chip tone="bad" plain className="mt-1 uppercase">{warning.badge}</Chip> : null}
        </AlertBanner>
      ))}
    </div>
  );
}

/** The strip over the form: what Save sends, the unsaved mark, Discard, Save, and a status line for a refusal. */
export function RwToolbar({ state, save, className }: { state: RwFormState; save: RwSave; className?: string }) {
  const writing = useWriting(RW_WRITE);
  const connectionBusy = useConnectionBusy();
  const { dirty, discard } = state;
  return (
    <div className={className}>
      <div className="flex flex-wrap items-center gap-2">
        <p className="min-w-0 flex-1 text-[11.5px] text-t3">Save sends the whole form — settings, LAN hosts and routed subnets</p>
        {dirty ? <span className="text-[11px] font-semibold text-warn">● unsaved changes</span> : null}
        <Button size="sm" disabled={!dirty || writing} onClick={discard}>Discard</Button>
        <Button size="sm" variant="primary" disabled={!dirty || writing || connectionBusy} onClick={() => void save.save()}>{save.saving ? "Saving…" : "Save"}</Button>
      </div>
      <p role="status" className={save.formError ? "mt-2 whitespace-pre-wrap text-xs text-bad" : "sr-only"}>{save.formError}</p>
    </div>
  );
}

/** The editor over one remote-access read. */
function RemoteAccessEditor({ rw }: { rw: Rw }) {
  const state = useRwForm(rw);
  const save = useRwSave(rw, state);
  const locked = useWriting(RW_WRITE);
  const hosts = useWatch({ control: state.form.control, name: "hosts" });
  const filledHosts = hosts.filter((row) => row.name.trim() !== "" || row.ip.trim() !== "").length;
  return (
    <>
      <RwToolbar state={state} save={save} className="glass bg-solid p-2.5" />
      <p role="status" className={state.gatewayChanged ? "glass border-warn/40 p-3 text-sm text-warn" : "sr-only"}>{state.gatewayChanged ? RW_GATEWAY_CHANGED : null}</p>
      <RwAlerts rw={rw} state={state} />
      <div className="grid items-start gap-3 md:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
        <GlassCard aria-label="Remote Access Inbound" className="flex min-w-0 flex-col gap-3">
          <CardHeader title="Remote Access Inbound" detail={<Chip>VLESS · XTLS-Vision · Reality</Chip>} aside={rw.live ? <Chip tone="ok">live</Chip> : null} className="mb-0" />
          <EnableToggle rw={rw} state={state} disabled={locked} />
          <InboundFields rw={rw} state={state} disabled={locked} />
        </GlassCard>
        <div className="flex min-w-0 flex-col gap-3">
          <GlassCard aria-label="LAN hosts by name" className="flex flex-col gap-2.5">
            <CardHeader title="LAN hosts by name" detail="the collision-free way in" aside={<span className="font-mono text-[11px] text-t3">{filledHosts} / {MAX_HOSTS}</span>} className="mb-0" />
            <p className="text-xs leading-relaxed text-t2"><WithCode text={HOSTS_NOTE} /></p>
            <HostRows state={state} disabled={locked} />
          </GlassCard>
          <GlassCard aria-label="Routed subnets" className="flex flex-col gap-2.5">
            <CardHeader title="Routed subnets" aside={<span className="text-[11px] text-t3">management /24 + segment /24</span>} className="mb-0" />
            <SubnetFields rw={rw} state={state} disabled={locked} />
          </GlassCard>
        </div>
      </div>
      <div className="grid items-start gap-3 md:grid-cols-[minmax(0,8fr)_minmax(0,4fr)]">
        <GlassCard aria-label="Router checklist" className="md:col-start-2">
          <CardHeader title="Router checklist" detail="not automated" />
          <RwChecklist port={rw.port} />
        </GlassCard>
      </div>
    </>
  );
}

/**
 * Gateway › Remote access (A1–A8): the polling owner of the remote-access read while mounted, every 15 s — paused while
 * one of its writes runs — and refetched when the tunnel's node or running state changes, which moves `live`.
 */
export function RemoteAccess() {
  const writing = useWriting(RW_WRITE);
  const rw = usePolledQuery(queries.rw(), writing ? 0 : RW_POLL_MS);
  const status = useQuery(queries.status());
  useRefetchOnChange(status.data ? `${status.data.active_node_id}|${status.data.running}` : null, RW_KEYS);
  return (
    <div className="flex flex-col gap-3">
      {rw.data && !writing ? staleNotice([rw], "Remote access did not refresh") : null}
      {rw.data ? (
        <RemoteAccessEditor rw={rw.data} />
      ) : rw.isError ? (
        <ErrorState message="Remote access did not load" onRetry={() => void rw.refetch()} />
      ) : (
        <div aria-busy className="grid gap-3 md:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
          <Skeleton className="h-96" />
          <Skeleton className="h-96" />
        </div>
      )}
    </div>
  );
}
