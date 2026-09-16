import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useState } from "react";
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
import { DESKTOP_QUERY, useMediaQuery } from "../../lib/media";
import { EditorSection } from "../tunnel/EditorSection";
import { AddClientForm, AddClientSheet, CLIENTS_NOTE, ClientCards, ClientsTable } from "./Clients";
import { HOSTS_NOTE, EnableToggle, HostRows, InboundFields, RwChecklist, SubnetFields, WithCode } from "./RwFields";
import { MAX_CLIENTS, MAX_HOSTS, focusIndexAfterRemove, rwWarnings } from "./rwForm";
import { useRwClientActions, type RwClientActions } from "./useRwClientActions";
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

/**
 * Focus after a client is removed: the row that took its place, else the one before it, else the add field — never
 * the page itself. The row's own buttons are gone, and focus would otherwise fall to <body>.
 */
function focusAfterRemove(client: { email: string }) {
  // Rows are found by name — names are unique on a gateway, and a uuid must never be in an attribute.
  const rows = [...document.querySelectorAll<HTMLElement>("[data-client]")];
  const index = rows.findIndex((row) => row.dataset.client === client.email);
  window.setTimeout(() => {
    const target = focusIndexAfterRemove(index, rows.length);
    const row = target !== null ? document.querySelectorAll<HTMLElement>("[data-client]")[target] : undefined;
    (row?.querySelector<HTMLElement>("[data-client-actions] button") ?? document.querySelector<HTMLElement>("[data-add-client]"))?.focus();
  }, 0);
}

interface RwParts {
  rw: Rw;
  state: RwFormState;
  save: RwSave;
  clientActions: RwClientActions;
  locked: boolean;
  filledHosts: number;
}

/** The editor over one remote-access read, laid out for a desktop or a phone. */
function RemoteAccessEditor({ rw }: { rw: Rw }) {
  const desktop = useMediaQuery(DESKTOP_QUERY);
  const state = useRwForm(rw);
  const save = useRwSave(rw, state);
  const locked = useWriting(RW_WRITE);
  const hosts = useWatch({ control: state.form.control, name: "hosts" });
  const clientActions = useRwClientActions({ onRemoved: focusAfterRemove });
  const parts: RwParts = { rw, state, save, clientActions, locked, filledHosts: hosts.filter((row) => row.name.trim() !== "" || row.ip.trim() !== "").length };
  return desktop ? <RwDesktop {...parts} /> : <RwPhone {...parts} />;
}

function GatewayChangedNotice({ state }: { state: RwFormState }) {
  return <p role="status" className={state.gatewayChanged ? "glass border-warn/40 p-3 text-sm text-warn" : "sr-only"}>{state.gatewayChanged ? RW_GATEWAY_CHANGED : null}</p>;
}

/** Desktop: the toolbar, the alerts, the inbound beside hosts and subnets, then the clients beside the checklist. */
function RwDesktop({ rw, state, save, clientActions, locked, filledHosts }: RwParts) {
  return (
    <>
      <RwToolbar state={state} save={save} className="glass bg-solid p-2.5" />
      <GatewayChangedNotice state={state} />
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
        <GlassCard aria-label="Clients" className="flex min-w-0 flex-col gap-2.5">
          <CardHeader title="Clients" detail="one per device" aside={<span className="font-mono text-[11px] text-t3">{rw.clients.length} / {MAX_CLIENTS}</span>} className="mb-0" />
          <p className="text-xs leading-relaxed text-t2">{CLIENTS_NOTE}</p>
          <ClientsTable clients={rw.clients} actions={clientActions} />
          <AddClientForm actions={clientActions} count={rw.clients.length} />
        </GlassCard>
        <GlassCard aria-label="Router checklist">
          <CardHeader title="Router checklist" detail="not automated" />
          <RwChecklist port={rw.port} />
        </GlassCard>
      </div>
    </>
  );
}

type PhoneSection = "hosts" | "subnets" | "checklist";

/**
 * Phone: the alerts, the enable switch, the clients as cards with Add client in a sheet, the inbound fields, then
 * collapsible hosts, subnets and checklist — a section holding an error stays open — and a sticky Save while edited.
 */
function RwPhone({ rw, state, save, clientActions, locked, filledHosts }: RwParts) {
  const [adding, setAdding] = useState(false);
  const [toggled, setToggled] = useState<Record<PhoneSection, boolean>>({ hosts: false, subnets: false, checklist: false });
  const { errors } = state.form.formState;
  const failing: Record<PhoneSection, boolean> = { hosts: errors.hosts !== undefined, subnets: errors.routedNets !== undefined, checklist: false };
  const section = (name: PhoneSection) => {
    const open = toggled[name] || failing[name];
    return { collapsible: true, open, onToggle: () => setToggled((current) => ({ ...current, [name]: !open })) };
  };
  return (
    <div className="flex flex-col gap-3 [&_input]:scroll-mb-44">
      <GatewayChangedNotice state={state} />
      <RwAlerts rw={rw} state={state} />
      <GlassCard aria-label="Remote access" className="flex flex-col gap-2">
        <CardHeader title="Remote access" aside={rw.live ? <Chip tone="ok">live</Chip> : null} className="mb-0" />
        <EnableToggle rw={rw} state={state} disabled={locked} />
      </GlassCard>
      <section aria-label="Clients" className="flex flex-col gap-2">
        <div className="flex items-center gap-2 px-1">
          <h2 className="text-[15px] font-bold text-t1">Clients</h2>
          <span className="font-mono text-[11px] text-t3">{rw.clients.length} / {MAX_CLIENTS}</span>
          <Button size="sm" data-add-client className="ml-auto" disabled={locked || rw.clients.length >= MAX_CLIENTS} onClick={() => setAdding(true)}><Plus size={14} aria-hidden />Add client</Button>
        </div>
        <ClientCards clients={rw.clients} actions={clientActions} />
      </section>
      {adding ? <AddClientSheet actions={clientActions} count={rw.clients.length} onClose={() => setAdding(false)} /> : null}
      <GlassCard className="flex flex-col px-4 py-1">
        <EditorSection title="Remote Access Inbound" note="VLESS · XTLS-Vision · Reality" collapsible={false} open onToggle={() => {}}>
          <InboundFields rw={rw} state={state} disabled={locked} />
        </EditorSection>
        <EditorSection title="LAN hosts by name" note="the collision-free way in" summary={`${filledHosts} / ${MAX_HOSTS}`} {...section("hosts")}>
          <p className="text-xs leading-relaxed text-t2"><WithCode text={HOSTS_NOTE} /></p>
          <HostRows state={state} disabled={locked} />
        </EditorSection>
        <EditorSection title="Routed subnets" note="management /24 + segment /24" summary={rw.routed_nets.join(", ")} {...section("subnets")}>
          <SubnetFields rw={rw} state={state} disabled={locked} />
        </EditorSection>
        <EditorSection title="Router checklist" note="not automated" summary="3 steps" {...section("checklist")}>
          <RwChecklist port={rw.port} collapsible />
        </EditorSection>
      </GlassCard>
      <div className={state.dirty ? "glass sticky bottom-24 z-20 bg-solid p-2.5" : "contents"}>
        {state.dirty ? (
          <div className="flex items-center justify-end gap-2">
            <span className="mr-auto text-[11px] font-semibold text-warn">● unsaved changes</span>
            <Button size="sm" disabled={locked} onClick={state.discard}>Discard</Button>
            <PhoneSave save={save} />
          </div>
        ) : null}
        <p role="status" className={save.formError ? "mt-2 whitespace-pre-wrap text-xs text-bad" : "sr-only"}>{save.formError}</p>
      </div>
    </div>
  );
}

function PhoneSave({ save }: { save: RwSave }) {
  const writing = useWriting(RW_WRITE);
  const connectionBusy = useConnectionBusy();
  return <Button size="sm" variant="primary" disabled={writing || connectionBusy} onClick={() => void save.save()}>{save.saving ? "Saving…" : "Save"}</Button>;
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
