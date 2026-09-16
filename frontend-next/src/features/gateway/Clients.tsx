import { Copy, Download, Eye, EyeOff, Plus } from "lucide-react";
import { memo, useId, useState, type FormEvent } from "react";
import type { RwClient } from "../../api/client";
import { RW_WRITE, useConnectionBusy, useWriting } from "../../api/invalidation";
import { Chip } from "../../components/data/Chip";
import { Button } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import { cn } from "../../lib/cn";
import { maskUuid } from "./revocation";
import { clientNameIssue } from "./rwForm";
import type { RwClientActions, RwClientCallbacks } from "./useRwClientActions";

export const MAX_CLIENTS = 16;

export const CLIENTS_NOTE = (
  <>
    <b className="text-t1">Suspend</b> revokes a device now and keeps its uuid — for a lost phone, where Remove would mean reissuing everything.{" "}
    <b className="text-t1">.conf</b> is the full Shadowrocket config — routing rules included, so LAN access works without hand-editing anything. Download it
    while you are on this network; there is deliberately no public subscription URL. <b className="text-t1">Copy link</b> is the plain{" "}
    <code className="font-mono text-t1">vless://</code> fallback for any other client.
  </>
);

/**
 * A client uuid is a credential: its first four characters and dots until Reveal, which turns into Hide on the same
 * button. The whole uuid is in the page only while revealed — never in a title or an attribute — and a row hides it
 * again whenever the screen is left (the state is the row's own).
 */
export function MaskedUuid({ client }: { client: RwClient }) {
  const [revealed, setRevealed] = useState(false);
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5">
      {revealed ? (
        <span className="break-all font-mono text-xs text-t1">{client.id}</span>
      ) : (
        <span className="font-mono text-xs text-t2">
          <span aria-hidden>{maskUuid(client.id)}</span>
          <span className="sr-only">uuid hidden</span>
        </span>
      )}
      <Button size="sm" variant="ghost" aria-label={`Reveal uuid of ${client.email}`} aria-pressed={revealed} className="h-6 px-1.5 text-[11px]" onClick={() => setRevealed((value) => !value)}>
        {revealed ? <EyeOff size={12} aria-hidden /> : <Eye size={12} aria-hidden />}
        {revealed ? "Hide" : "Reveal"}
      </Button>
    </span>
  );
}

export function ClientStatus({ client }: { client: RwClient }) {
  return client.enabled ? <Chip tone="ok">active</Chip> : <Chip tone="neutral" plain className="font-semibold uppercase tracking-wide">suspended</Chip>;
}

/** Busy flags every row reads: any remote-access write (it also holds the reads), and any connection write for the writes. */
export function useClientBusy() {
  const writing = useWriting(RW_WRITE);
  const connectionBusy = useConnectionBusy();
  return { reads: writing, writes: writing || connectionBusy };
}

const ClientRow = memo(function ClientRow({ client, actions, reads, writes }: { client: RwClient; actions: RwClientCallbacks; reads: boolean; writes: boolean }) {
  return (
    <tr data-client={client.email} className="border-t border-line">
      <td className="py-2 pr-3 text-[13px] font-semibold text-t1">{client.email}</td>
      <td className="py-2 pr-3"><ClientStatus client={client} /></td>
      <td className="py-2 pr-3"><MaskedUuid client={client} /></td>
      <td className="py-2">
        <div data-client-actions className="flex flex-wrap justify-end gap-1.5">
          {client.enabled ? (
            <Button size="sm" aria-label={`Suspend ${client.email}`} disabled={writes} onClick={() => actions.onSuspend(client)}>Suspend</Button>
          ) : (
            <Button size="sm" aria-label={`Resume ${client.email}`} disabled={writes} onClick={() => actions.onResume(client)}>Resume</Button>
          )}
          <Button size="sm" aria-label={`Download .conf for ${client.email}`} disabled={reads} onClick={() => actions.onConfig(client)}><Download size={13} aria-hidden />.conf</Button>
          <Button size="sm" aria-label={`Copy link for ${client.email}`} disabled={reads} onClick={() => actions.onCopyLink(client)}><Copy size={13} aria-hidden />Copy link</Button>
          <Button size="sm" variant="danger" aria-label={`Remove ${client.email}`} disabled={writes} onClick={() => actions.onRemove(client)}>Remove</Button>
        </div>
      </td>
    </tr>
  );
});

/** Desktop: the clients as a real table with column headers; every row's actions named with the client. */
export function ClientsTable({ clients, actions }: { clients: readonly RwClient[]; actions: RwClientCallbacks }) {
  const busy = useClientBusy();
  if (clients.length === 0) return <p className="py-2 text-sm text-t3">No clients yet.</p>;
  return (
    <div className="overflow-x-auto">
      <table aria-label="Clients" className="w-full text-left">
        <thead>
          <tr className="text-[10.5px] font-semibold uppercase tracking-wide text-t3">
            <th scope="col" className="pb-1.5 pr-3 font-semibold">Name</th>
            <th scope="col" className="pb-1.5 pr-3 font-semibold">Status</th>
            <th scope="col" className="pb-1.5 pr-3 font-semibold">UUID</th>
            <th scope="col" className="pb-1.5 text-right font-semibold">Actions</th>
          </tr>
        </thead>
        <tbody>
          {clients.map((client) => <ClientRow key={client.id} client={client} actions={actions} reads={busy.reads} writes={busy.writes} />)}
        </tbody>
      </table>
    </div>
  );
}

/**
 * The add field: a name the gateway accepts (1–40 letters, digits, dot, dash or underscore), checked before anything
 * is sent; Enter submits. The field clears once the client exists.
 */
export function AddClientForm({ actions, count, onAdded, className }: { actions: Pick<RwClientActions, "add">; count: number; onAdded?: () => void; className?: string }) {
  const id = useId();
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const busy = useClientBusy();

  async function submit(event: FormEvent) {
    event.preventDefault();
    const issue = clientNameIssue(name);
    setError(issue);
    if (issue) return;
    if (await actions.add(name.trim())) {
      setName("");
      onAdded?.();
    }
  }

  return (
    <form onSubmit={(event) => void submit(event)} className={cn("flex flex-col gap-1", className)}>
      <div className="flex items-center gap-2">
        <label htmlFor={id} className="sr-only">Client name</label>
        <Input
          id={id} data-add-client value={name} placeholder="iphone" autoComplete="off" spellCheck={false} className="font-mono" disabled={busy.writes || count >= MAX_CLIENTS}
          aria-invalid={error ? true : undefined} aria-describedby={error ? `${id}-error` : undefined}
          onChange={(event) => { setName(event.target.value); setError(null); }}
        />
        <Button type="submit" size="md" disabled={busy.writes || count >= MAX_CLIENTS}><Plus size={14} aria-hidden />Add</Button>
      </div>
      {error ? <p id={`${id}-error`} className="text-[11px] text-bad">{error}</p> : <p className="text-[11px] text-t3">Enter submits · 1–40 letters, digits, dot, dash or underscore</p>}
    </form>
  );
}
