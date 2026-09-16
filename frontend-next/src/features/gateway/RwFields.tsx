import { Copy, Plus, Sparkles, X } from "lucide-react";
import { useRef, useState, type ReactNode } from "react";
import { Controller, useFieldArray, useWatch } from "react-hook-form";
import { api, type Rw } from "../../api/client";
import { Chip } from "../../components/data/Chip";
import { StepList } from "../../components/data/StepList";
import { Button } from "../../components/ui/Button";
import { TextField } from "../../components/ui/Field";
import { Input } from "../../components/ui/Input";
import { Toggle } from "../../components/ui/Toggle";
import { notifyError, notifyOk } from "../../components/ui/Toaster";
import { copyText } from "../../lib/clipboard";
import { cn } from "../../lib/cn";
import { CsvChipsField } from "./CsvChipsField";
import { Help } from "./NetworkFields";
import { MAX_HOSTS, canTurnOn, destHost, parseCsv, sniMismatch } from "./rwForm";
import type { RwFormState } from "./useRwForm";

/** Text whose `backticked` parts are code, as the contract writes them. */
export function WithCode({ text }: { text: string }): ReactNode {
  return text.split("`").map((part, index) => (index % 2 === 1 ? <code key={index} className="font-mono text-t1">{part}</code> : part));
}

export const KEY_NOTE =
  "Generate the pair once on the gateway — `docker exec v2pi xray x25519` — and paste both halves here. The panel keeps the private key on the gateway and never returns it to the browser after it is set, and it is deliberately left out of backups — after restoring onto a new host, paste it again.";

/** A2: the enable switch. On needs a key, stored or typed; off is always allowed. */
export function EnableToggle({ rw, state, disabled }: { rw: Rw; state: RwFormState; disabled: boolean }) {
  const { control, setValue } = state.form;
  const [enabled, privateKey] = useWatch({ control, name: ["enabled", "privateKey"] });
  const blocked = !enabled && !canTurnOn(rw, { privateKey });
  return (
    <div className="flex items-start gap-3">
      <div className="pt-0.5">
        <Toggle label="Accept inbound connections" checked={enabled} disabled={disabled || blocked} onCheckedChange={(on) => setValue("enabled", on, { shouldDirty: true, shouldValidate: true })} />
      </div>
      <div className="min-w-0 flex-1">
        <p className="text-sm font-semibold text-t1">Accept inbound connections from outside.</p>
        <p className="text-xs leading-relaxed text-t3">LAN access keeps working when the exit node is down — <code>private → direct</code> does not depend on the tunnel.</p>
      </div>
      {rw.has_private_key ? <Chip className="shrink-0">private key <b>STORED</b></Chip> : null}
    </div>
  );
}

/** A3: port, endpoint, dest, public key, server names, short ids with Generate, and the write-only private key. */
export function InboundFields({ rw, state, disabled }: { rw: Rw; state: RwFormState; disabled: boolean }) {
  const { register, control, getValues, setValue, formState: { errors } } = state.form;
  const [dest, serverNames, publicKey] = useWatch({ control, name: ["dest", "serverNames", "publicKey"] });
  const [generating, setGenerating] = useState(false);
  const mismatch = sniMismatch({ dest, serverNames });

  async function generate() {
    setGenerating(true);
    try {
      const { short_id: shortId } = await api.newRwShortId();
      setValue("shortIds", [...getValues("shortIds"), shortId], { shouldDirty: true, shouldValidate: true });
      notifyOk("short id added — save to apply");
    } catch {
      notifyError(null, "could not generate a short id");
    } finally {
      setGenerating(false);
    }
  }

  async function copyPublicKey() {
    try {
      await copyText(getValues("publicKey").trim());
      notifyOk("public key copied");
    } catch {
      notifyError(null, "copy failed");
    }
  }

  return (
    <div className="grid gap-x-3 gap-y-2.5 sm:grid-cols-2">
      <div className="flex flex-col gap-1">
        <TextField label="Listen port" inputMode="numeric" autoComplete="off" className="font-mono" disabled={disabled} error={errors.port?.message} {...register("port")} />
        {errors.port ? null : <Help>integer 1–65535</Help>}
      </div>
      <div className="flex flex-col gap-1">
        <TextField label="External endpoint" hint="(DDNS name or WAN IP)" placeholder="home.example.org" autoComplete="off" spellCheck={false} className="font-mono" disabled={disabled} error={errors.endpoint?.message} {...register("endpoint")} />
        {errors.endpoint ? null : <Help>host name, IPv4 or [IPv6]</Help>}
      </div>
      <div className="flex flex-col gap-1">
        <TextField label="Reality dest" hint="(the site probes get)" placeholder="www.microsoft.com:443" autoComplete="off" spellCheck={false} className="font-mono" disabled={disabled} error={errors.dest?.message} {...register("dest")} />
        {errors.dest ? null : <Help>host:port · stored normalised</Help>}
      </div>
      <div className="flex flex-col gap-1">
        <div className="flex items-end gap-2">
          <TextField label="Public key" hint="(goes in client links)" autoComplete="off" spellCheck={false} fieldClassName="flex-1" className="font-mono text-xs" disabled={disabled} error={errors.publicKey?.message} {...register("publicKey")} />
          <Button size="icon" aria-label="Copy public key" title="Copy public key" disabled={publicKey.trim() === ""} onClick={() => void copyPublicKey()} className={errors.publicKey ? "mb-5" : undefined}>
            <Copy size={15} aria-hidden />
          </Button>
        </div>
        {errors.publicKey ? null : <Help>base64 x25519 · 43 characters</Help>}
      </div>
      <div className="flex flex-col gap-1 sm:col-span-2">
        <Controller
          control={control}
          name="serverNames"
          render={({ field }) => (
            <CsvChipsField
              label="Server names" hint="(SNI, must match dest)" placeholder="www.microsoft.com" item="server name" values={field.value}
              saved={parseCsv(rw.server_names)} onChange={field.onChange} error={errors.serverNames?.message} warn={mismatch} disabled={disabled}
            />
          )}
        />
        {mismatch && !errors.serverNames ? <p className="text-[11px] text-warn">dest host is {destHost({ dest })} — it is not in this list</p> : null}
      </div>
      <div className="flex flex-col gap-1 sm:col-span-2">
        <Controller
          control={control}
          name="shortIds"
          render={({ field }) => (
            <CsvChipsField
              label="Short ids" hint="(csv hex, even length)" placeholder="ab12cd34" item="short id" values={field.value} saved={parseCsv(rw.short_ids)}
              onChange={field.onChange} error={errors.shortIds?.message} disabled={disabled}
              action={<Button disabled={disabled || generating} onClick={() => void generate()}><Sparkles size={14} aria-hidden />Generate</Button>}
            />
          )}
        />
        {errors.shortIds ? null : <Help>2–16 hex chars · the generated id is highlighted and not stored until Save</Help>}
      </div>
      <div className="flex flex-col gap-1">
        <TextField
          label="Private key"
          hint={rw.has_private_key ? "stored — leave blank to keep it" : "required before enabling"}
          type="password"
          autoComplete="off"
          placeholder={rw.has_private_key ? "•••••••• stored" : "paste the private key from `xray x25519`"}
          className="font-mono"
          disabled={disabled}
          error={errors.privateKey?.message}
          {...register("privateKey")}
        />
        {errors.privateKey ? null : <Help>password field · write-only · never shown or exported</Help>}
      </div>
      <p className="rounded-xl border border-line bg-glass px-3 py-2 text-xs leading-relaxed text-t2"><WithCode text={KEY_NOTE} /></p>
    </div>
  );
}

export const HOSTS_NOTE =
  "A remote client that routes `192.168.1.0/24` into the tunnel collides with every cafe running the same prefix, and `192.168.1.88` can silently mean their printer. A name cannot collide. The gateway resolves these, so the client only needs one rule per suffix. Avoid `.local` — iOS and macOS answer it over mDNS and it never reaches the tunnel.";

/** A5: LAN hosts by name — rows of a name and an IPv4 address, sent with Save. */
export function HostRows({ state, disabled }: { state: RwFormState; disabled: boolean }) {
  const { control, register, setFocus, trigger, formState: { errors } } = state.form;
  const { fields, append, remove } = useFieldArray({ control, name: "hosts" });
  const addRef = useRef<HTMLButtonElement>(null);
  const hostsError = errors.hosts?.message ?? errors.hosts?.root?.message;

  // A row's checks span both of its fields and the rows around it (a duplicate name): re-check every row on any change.
  const deps = fields.flatMap((_row, index) => [`hosts.${index}.name`, `hosts.${index}.ip`] as const);

  function removeRow(index: number) {
    const remaining = fields.length - 1;
    remove(index);
    // The rows left are checked again (a duplicate may be gone). Focus stays in the list: the row that took this one's
    // place, else the one before it, else Add host.
    window.setTimeout(() => {
      void trigger("hosts");
      if (remaining > 0) setFocus(`hosts.${Math.min(index, remaining - 1)}.name`);
      else addRef.current?.focus();
    }, 0);
  }

  return (
    <div className="flex flex-col gap-2">
      {fields.length > 0 ? (
        <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] gap-x-2 gap-y-1.5">
          <span aria-hidden className="text-[10.5px] font-semibold tracking-wide text-t3">Name</span>
          <span aria-hidden className="text-[10.5px] font-semibold tracking-wide text-t3">IPv4</span>
          <span />
          {fields.map((row, index) => {
            const rowErrors = errors.hosts?.[index];
            const message = rowErrors?.name?.message ?? rowErrors?.ip?.message;
            const errorId = `${row.id}-error`;
            return (
              <div key={row.id} className="contents">
                <Input aria-label={`Host ${index + 1} name`} placeholder="nas.v2pi" autoComplete="off" spellCheck={false} className={cn("font-mono", rowErrors?.name && "border-bad/60")} disabled={disabled} aria-invalid={rowErrors?.name ? true : undefined} aria-describedby={rowErrors?.name ? errorId : undefined} {...register(`hosts.${index}.name`, { deps })} />
                <Input aria-label={`Host ${index + 1} IPv4`} placeholder="192.168.1.88" inputMode="decimal" autoComplete="off" className={cn("font-mono", rowErrors?.ip && "border-bad/60")} disabled={disabled} aria-invalid={rowErrors?.ip ? true : undefined} aria-describedby={rowErrors?.ip ? errorId : undefined} {...register(`hosts.${index}.ip`, { deps })} />
                <Button size="icon" variant="ghost" aria-label={`Remove host ${index + 1}`} disabled={disabled} onClick={() => removeRow(index)}>
                  <X size={15} aria-hidden />
                </Button>
                {message ? <p id={errorId} className="col-span-3 -mt-0.5 text-[11px] text-bad">{message}</p> : null}
              </div>
            );
          })}
        </div>
      ) : null}
      {hostsError ? <p className="text-[11px] text-bad">{hostsError}</p> : null}
      <div className="flex flex-wrap items-center gap-2">
        <Button ref={addRef} size="sm" variant="ghost" disabled={disabled || fields.length >= MAX_HOSTS} onClick={() => append({ name: "", ip: "" }, { shouldFocus: true })}>
          <Plus size={14} aria-hidden />Add host
        </Button>
        <span className="text-[11px] text-t3">dotted name, not .local · IPv4 only</span>
      </div>
    </div>
  );
}

export const SUBNETS_NOTE = "Pushed into the generated client config. Derived from the live network plan, so they follow your addressing instead of a hardcoded guess.";

/** A7: the subnets clients route into the tunnel (saved), and the override sent with Save. */
export function SubnetFields({ rw, state, disabled }: { rw: Rw; state: RwFormState; disabled: boolean }) {
  const { register, formState: { errors } } = state.form;
  return (
    <div className="flex flex-col gap-2.5">
      <p className="text-xs leading-relaxed text-t2">{SUBNETS_NOTE}</p>
      <ul aria-label="Routed subnets" className="flex flex-wrap gap-1.5">
        {rw.routed_nets.map((net) => <li key={net} className="rounded-lg border border-line bg-glass px-2 py-0.5 font-mono text-xs text-t1">{net}</li>)}
      </ul>
      <div className="flex flex-col gap-1">
        <TextField label="Override" hint="(csv, blank = derive)" placeholder="192.168.1.0/24,192.168.10.0/24" autoComplete="off" spellCheck={false} className="font-mono" disabled={disabled} error={errors.routedNets?.message} {...register("routedNets")} />
        {errors.routedNets ? null : <Help>IPv4 CIDRs · host bits allowed · deduped</Help>}
      </div>
    </div>
  );
}

/** A8: the router steps for remote access, with the saved port — static guidance, never verified. */
export function RwChecklist({ port, collapsible = false }: { port: number; collapsible?: boolean }) {
  return (
    <StepList
      collapsible={collapsible}
      steps={[
        { key: "port-forward", title: <><b>Port-forward</b> WAN :{port} → this gateway, <i>on every WAN link</i></>, detail: "a failover that swaps providers must not drop the rule." },
        { key: "ddns", title: <b>DDNS in direct mode.</b>, detail: "A cloud/proxied DDNS mode forwards HTTP(S) only and will not carry Reality's raw TCP." },
        { key: "port-free", title: <b>Check :{port} is free on the router itself</b>, detail: "its own web UI or remote-access service often sits on 443." },
      ]}
    />
  );
}
