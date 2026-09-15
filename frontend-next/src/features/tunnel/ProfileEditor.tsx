import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useId, useState, type ReactNode } from "react";
import { Controller, useWatch } from "react-hook-form";
import { api, errText, type ProfileIn, type TuningProfile } from "../../api/client";
import {
  CONNECTION_BUSY, PROFILE_CONNECTION_WRITE, PROFILE_WRITE, isConnectionBusy, useApiWrite, useConnectionBusy, useProfileBusy,
} from "../../api/invalidation";
import { keys } from "../../api/keys";
import { CheckLine } from "../../components/data/CheckLine";
import { Button } from "../../components/ui/Button";
import { FieldShell, SegmentedField, TextField } from "../../components/ui/Field";
import { Select } from "../../components/ui/Select";
import { Toggle } from "../../components/ui/Toggle";
import { notifyError, notifyOk } from "../../components/ui/Toaster";
import { cn } from "../../lib/cn";
import { checkResult, runKey, type CheckResult } from "../../lib/staleResult";
import { EditorSection } from "./EditorSection";
import { NoiseRows } from "./NoiseRows";
import { PresetMenu } from "./PresetMenu";
import {
  FINGERPRINTS, MAX_ALPN, MAX_NAME, MAX_TLS_VERSION, NO_MIMICRY, QUIC_LABELS, QUIC_MODES, XUDP_MODES, formToProfileIn, profileFormSchema,
  profileSavedMessage, sectionStates, type ProfileFormValues, type ProfileSection,
} from "./profileForm";
import type { ProfileEditorState } from "./useProfileEditor";

const QUIC_OPTIONS = QUIC_MODES.map((mode) => ({ value: mode, label: QUIC_LABELS[mode] }));

/** A section's on/off switch, as a row under its header. */
function SwitchRow({ label, text, children }: { label: string; text: string; children: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-xl border border-line bg-glass px-3 py-2">
      <p className="text-sm text-t1">{text}<span className="sr-only"> ({label})</span></p>
      {children}
    </div>
  );
}

interface SaveVariables {
  id: number | null;
  body: ProfileIn;
}

/**
 * T5: Validate, Create / Save and the preset menu under the fields. Validate is a peek, marked stale once the form
 * moves past what it checked. Saving the profile the live tunnel runs on re-applies it, so that is a connection write.
 */
function EditorFooter({ editor, sticky }: { editor: ProfileEditorState; sticky: boolean }) {
  const queryClient = useQueryClient();
  const { form, editing } = editor;
  const values = useWatch({ control: form.control }) as ProfileFormValues;
  const liveKey = runKey(formToProfileIn(values));
  const [check, setCheck] = useState<CheckResult | null>(null);
  const [validating, setValidating] = useState(false);
  const busy = useProfileBusy();
  const connectionBusy = useConnectionBusy();
  const addWrite = useApiWrite("addProfile");
  const updateWrite = useApiWrite("updateProfile");
  const write = ({ id, body }: SaveVariables) => (id === null ? addWrite(body) : updateWrite(id, body));
  // What a save did is said even if the editor has gone; resetting the editor is passed to mutate below.
  const saveOptions = {
    mutationFn: write,
    onSuccess: (saved: TuningProfile) => notifyOk(profileSavedMessage(saved)),
    onError: (error: Error) => notifyError(error, "save failed"),
  };
  const savePlain = useMutation({ mutationKey: PROFILE_WRITE, ...saveOptions });
  const saveLive = useMutation({ mutationKey: PROFILE_CONNECTION_WRITE, ...saveOptions });
  const existing = typeof editing === "number";
  const live = existing && (queryClient.getQueryData<TuningProfile[]>(keys.profiles)?.find((profile) => profile.id === editing)?.is_active ?? false);
  const saving = savePlain.isPending || saveLive.isPending;

  async function validate() {
    const current = form.getValues();
    const key = runKey(formToProfileIn(current));
    const parsed = profileFormSchema.safeParse(current);
    if (!parsed.success) {
      void form.trigger();
      setCheck({ ok: false, text: `✗ ${parsed.error.issues[0]!.message}`, key });
      return;
    }
    setValidating(true);
    try {
      setCheck(checkResult(await api.validateProfile(formToProfileIn(parsed.data)), "profile valid", key));
    } catch (error) {
      setCheck({ ok: false, text: `✗ ${errText(error, "validate failed")}`, key });
    } finally {
      setValidating(false);
    }
  }

  const submit = form.handleSubmit((submitted) => {
    const id = typeof editing === "number" ? editing : null;
    // Whether the save re-applies the tunnel, as the list says now.
    const nowLive = id !== null && (queryClient.getQueryData<TuningProfile[]>(keys.profiles)?.find((profile) => profile.id === id)?.is_active ?? false);
    if (nowLive && isConnectionBusy(queryClient)) {
      notifyError(null, CONNECTION_BUSY);
      return;
    }
    (nowLive ? saveLive : savePlain).mutate({ id, body: formToProfileIn(submitted) }, { onSuccess: () => editor.reset(false) });
  });

  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-2 border-t border-line bg-solid pt-3",
        sticky ? "glass sticky bottom-24 z-20 border-t-0 p-2.5" : "min-[1180px]:sticky min-[1180px]:bottom-0 min-[1180px]:z-10 min-[1180px]:pb-1",
      )}
    >
      {sticky ? <PresetMenu editor={editor} compact /> : <PresetMenu editor={editor} />}
      <div className={cn("min-w-0 flex-1", !sticky && "basis-32")}>
        <CheckLine result={check} liveKey={liveKey} />
      </div>
      <Button size="sm" disabled={validating} onClick={() => void validate()}>{validating ? "Validating…" : "Validate"}</Button>
      <Button size="sm" variant="primary" disabled={saving || busy || (live && connectionBusy)} onClick={() => void submit()}>
        {saving ? "Saving…" : existing ? "Save" : "Create"}
      </Button>
    </div>
  );
}

/**
 * T3: the profile's fields in contract order, each section a fieldset. On a desktop every section is open; on a phone
 * the sections collapse, opening those that hold a feature that is on. Keyed by the editor's generation, so loading
 * other values starts the sections and the Validate result over.
 */
export function ProfileEditor({ editor, desktop }: { editor: ProfileEditorState; desktop: boolean }) {
  const id = useId();
  const { form } = editor;
  const { register, control, formState: { errors } } = form;
  const [fragEnabled, noiseEnabled, muxEnabled, dohEnabled] = useWatch({ control, name: ["frag_enabled", "noise_enabled", "mux_enabled", "doh_enabled"] });
  const watched = useWatch({ control }) as ProfileFormValues;
  const states = sectionStates(watched);
  const [open, setOpen] = useState<Record<ProfileSection, boolean>>(() => {
    const initial = sectionStates(form.getValues());
    return { fragmentation: initial.fragmentation.open, noise: initial.noise.open, mux: initial.mux.open, xhttp: initial.xhttp.open, tls: initial.tls.open, dns: initial.dns.open };
  });
  const section = (name: ProfileSection) => ({
    collapsible: !desktop,
    open: open[name],
    summary: states[name].summary,
    onToggle: () => setOpen((current) => ({ ...current, [name]: !current[name] })),
  });

  return (
    // A plain container, not a <form>: radix switches inside a form add hidden checkboxes of their own.
    <div className="flex flex-col">
      <EditorSection title="Profile" collapsible={false} open onToggle={() => {}}>
        <TextField label="Name" required hint={`1–${MAX_NAME}`} autoComplete="off" error={errors.name?.message} {...register("name")} />
        <FieldShell id={`${id}-fingerprint`} label="Fingerprint">
          <Select id={`${id}-fingerprint`} {...register("fingerprint")}>
            {FINGERPRINTS.map((fingerprint) => <option key={fingerprint} value={fingerprint}>{fingerprint === "" ? NO_MIMICRY : fingerprint}</option>)}
          </Select>
        </FieldShell>
      </EditorSection>

      <EditorSection title="TLS fragmentation" {...section("fragmentation")}>
        <SwitchRow label="TLS fragmentation" text="Split the TLS ClientHello">
          <Controller control={control} name="frag_enabled" render={({ field }) => <Toggle label="TLS fragmentation" checked={field.value} onCheckedChange={field.onChange} />} />
        </SwitchRow>
        {fragEnabled ? (
          <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
            <TextField label="Packets" hint="tlshello / 1-3" list={`${id}-packets`} autoComplete="off" className="font-mono text-xs" error={errors.frag_packets?.message} {...register("frag_packets")} />
            <TextField label="Length" hint="1..65535" placeholder="100-200" className="font-mono text-xs" error={errors.frag_length?.message} {...register("frag_length")} />
            <TextField label="Interval, ms" hint="0..60000" placeholder="10-20" className="font-mono text-xs" error={errors.frag_interval?.message} {...register("frag_interval")} />
            <datalist id={`${id}-packets`}><option value="tlshello" /><option value="1-3" /></datalist>
          </div>
        ) : null}
      </EditorSection>

      <EditorSection title="UDP noise" note="decoy packets vs DPI / active probing" {...section("noise")}>
        <SwitchRow label="UDP noise" text="Send decoy UDP packets">
          <Controller control={control} name="noise_enabled" render={({ field }) => <Toggle label="UDP noise" checked={field.value} onCheckedChange={field.onChange} />} />
        </SwitchRow>
        {noiseEnabled ? <NoiseRows control={control} register={register} errors={errors} /> : null}
        {errors.noises?.root?.message ? <p className="text-[11px] text-bad">{errors.noises.root.message}</p> : null}
      </EditorSection>

      <EditorSection title="Mux" note="xhttp nodes only — ignored on Vision" {...section("mux")}>
        <SwitchRow label="Mux" text="Multiplex connections">
          <Controller control={control} name="mux_enabled" render={({ field }) => <Toggle label="Mux" checked={field.value} onCheckedChange={field.onChange} />} />
        </SwitchRow>
        {muxEnabled ? (
          <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
            <TextField label="Concurrency" hint="1..1024" placeholder="(default)" inputMode="numeric" className="font-mono text-xs" error={errors.mux_concurrency?.message} {...register("mux_concurrency")} />
            <FieldShell id={`${id}-xudp`} label="xudpProxyUDP443" hint="reject · allow · skip">
              <Select id={`${id}-xudp`} {...register("xudp_proxy_udp443")}>
                {XUDP_MODES.map((mode) => <option key={mode} value={mode}>{mode === "" ? "(default)" : mode}</option>)}
              </Select>
            </FieldShell>
          </div>
        ) : null}
      </EditorSection>

      <EditorSection title="XHTTP transport" note="xhttp nodes only" {...section("xhttp")}>
        <TextField label="Padding" hint="range 0..1,000,000" placeholder="100-1000" className="font-mono text-xs" error={errors.xhttp_padding?.message} {...register("xhttp_padding")} />
        <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
          <TextField label="xmux max concurrency" hint="0..65535" placeholder="(off)" className="font-mono text-xs" error={errors.xmux_max_concurrency?.message} {...register("xmux_max_concurrency")} />
          <TextField label="xmux max connections" hint="0..65535" placeholder="(off)" className="font-mono text-xs" error={errors.xmux_max_connections?.message} {...register("xmux_max_connections")} />
        </div>
      </EditorSection>

      <EditorSection title="TLS" note="tls-mode nodes" {...section("tls")}>
        <TextField label="ALPN" hint={`≤ ${MAX_ALPN}`} placeholder="h2,http/1.1" className="font-mono text-xs" error={errors.alpn?.message} {...register("alpn")} />
        <div className="grid grid-cols-2 gap-2.5">
          <TextField label="Min version" hint={`≤ ${MAX_TLS_VERSION}`} placeholder="(default)" className="font-mono text-xs" error={errors.tls_min?.message} {...register("tls_min")} />
          <TextField label="Max version" hint={`≤ ${MAX_TLS_VERSION}`} placeholder="(default)" className="font-mono text-xs" error={errors.tls_max?.message} {...register("tls_max")} />
        </div>
      </EditorSection>

      <EditorSection title="DNS & QUIC" {...section("dns")}>
        <SwitchRow label="DoH" text="DNS over HTTPS">
          <Controller control={control} name="doh_enabled" render={({ field }) => <Toggle label="DoH" checked={field.value} onCheckedChange={field.onChange} />} />
        </SwitchRow>
        {dohEnabled ? (
          <TextField label="DoH URL" hint="https:// only, blank = default" placeholder="(default)" className="font-mono text-xs" error={errors.doh_url?.message} {...register("doh_url")} />
        ) : errors.doh_url ? <p className="text-[11px] text-bad">{errors.doh_url.message}</p> : null}
        <SegmentedField legend="QUIC" options={QUIC_OPTIONS} radio={register("quic")} />
      </EditorSection>

      <EditorFooter editor={editor} sticky={!desktop} />
    </div>
  );
}
