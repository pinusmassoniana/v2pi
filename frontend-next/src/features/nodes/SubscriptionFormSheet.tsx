import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Controller, useForm, useWatch } from "react-hook-form";
import { api, errText, type Preview, type PreviewNodes, type Subscription } from "../../api/client";
import { useApiWrite } from "../../api/invalidation";
import { queries } from "../../api/keys";
import { useUnsavedGuard } from "../../app/guard";
import { closeGuarded } from "../../components/confirm";
import { Button } from "../../components/ui/Button";
import { FieldShell, TextField } from "../../components/ui/Field";
import { Sheet, SheetContent } from "../../components/ui/Sheet";
import { Toggle } from "../../components/ui/Toggle";
import { notifyOk } from "../../components/ui/Toaster";
import { DryRunResult } from "./DryRunResult";
import { KeyValueRowsEditor } from "./KeyValueRowsEditor";
import { RequestPreview } from "./RequestPreview";
import { SELECT_CLASS } from "./ServersToolbar";
import { blankSubForm, buildInjection, formToSubIn, subFormSchema, subToForm, type SubFormValues } from "./subForm";

// previewSubNodes' own fetch is the slow one — the backend budgets 20 s for it; give the client a
// margin over that instead of racing it with the shared 20 s default (REQUEST_TIMEOUT_MS in client.ts).
const DRY_RUN_TIMEOUT_MS = 30_000;

/** What produced a preview or dry-run result: the URL and injection as they were at the moment it ran. */
interface PeekTarget { url: string; injection: ReturnType<typeof buildInjection> }
interface Peek<T> { busy: boolean; data: T | null; error: string | null; target: PeekTarget | null }
const IDLE: Peek<never> = { busy: false, data: null, error: null, target: null };

function targetKey(target: PeekTarget): string {
  return JSON.stringify(target);
}

/**
 * U5 edit and U7 add, with U8 Preview request and U9 Dry-run parse. Preview and dry-run are peeks: they change
 * nothing, so they invalidate nothing and never touch what was typed.
 */
export function SubscriptionFormSheet({ sub, onClose }: { sub?: Subscription; onClose: () => void }) {
  const edit = sub !== undefined;
  const [defaults] = useState<SubFormValues>(() => (edit ? subToForm(sub) : blankSubForm()));
  const { register, control, handleSubmit, formState, getValues, trigger } = useForm<SubFormValues>({
    resolver: zodResolver(subFormSchema),
    defaultValues: defaults,
  });
  const dirty = formState.isDirty;
  useUnsavedGuard(dirty);
  const profiles = useQuery({ ...queries.profiles(), enabled: edit });
  const addWrite = useApiWrite("addSub");
  const updateWrite = useApiWrite("updateSub");
  const [preview, setPreview] = useState<Peek<Preview>>(IDLE);
  const [dryRun, setDryRun] = useState<Peek<PreviewNodes>>(IDLE);
  const [saveError, setSaveError] = useState<string | null>(null);

  // What a fresh preview/dry-run would target right now — reactive, so an edit anywhere the peeks read from
  // (the URL, a header, a query row) is reflected without another click. Compared against each peek's own
  // target to tell a still-current result from one the form has moved past.
  const liveUrl = useWatch({ control, name: "url" });
  const liveHeaders = useWatch({ control, name: "headers" });
  const liveQueries = useWatch({ control, name: "queries" });
  const liveKey = targetKey({ url: (liveUrl ?? "").trim(), injection: buildInjection(liveHeaders ?? [], liveQueries ?? []) });
  const previewStale = preview.target !== null && targetKey(preview.target) !== liveKey;
  const dryRunStale = dryRun.target !== null && targetKey(dryRun.target) !== liveKey;

  const save = useMutation({
    mutationFn: (values: SubFormValues) => (edit ? updateWrite(sub.id, formToSubIn(values, true)) : addWrite(formToSubIn(values, false))),
    onSuccess: (saved) => {
      notifyOk(edit ? `Saved ${saved.name}` : `Added ${saved.name}`);
      onClose();
    },
    // 422 "url: …" (a bad scheme or a private address): the form stays as typed, with the reason in it.
    onError: (error) => setSaveError(errText(error, edit ? "save failed" : "add failed")),
  });

  /** The URL and injection as typed now, once the URL is there. */
  async function request(): Promise<PeekTarget | null> {
    if (!(await trigger("url"))) return null;
    const values = getValues();
    return { url: values.url.trim(), injection: buildInjection(values.headers, values.queries) };
  }

  async function runPreview() {
    const target = await request();
    // A validation failure means whatever is on screen is for a URL that no longer parses — never leave a
    // stale success sitting next to the new error.
    if (!target) { setPreview(IDLE); return; }
    setPreview({ busy: true, data: null, error: null, target });
    try {
      setPreview({ busy: false, data: await api.previewSub(target.url, target.injection), error: null, target });
    } catch (error) {
      setPreview({ busy: false, data: null, error: errText(error, "preview failed"), target });
    }
  }

  async function runDryRun() {
    const target = await request();
    if (!target) { setDryRun(IDLE); return; }
    setDryRun({ busy: true, data: null, error: null, target });
    try {
      setDryRun({ busy: false, data: await api.previewSubNodes(target.url, target.injection, DRY_RUN_TIMEOUT_MS), error: null, target });
    } catch (error) {
      setDryRun({ busy: false, data: null, error: errText(error, "dry-run failed"), target });   // 502 fetch / 422 parse
    }
  }

  const { errors } = formState;
  const submit = handleSubmit((values) => {
    setSaveError(null);
    save.mutate(values);
  });

  return (
    <Sheet open dirty={dirty} onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent title={edit ? `Edit · ${sub.name}` : "Add subscription"} className="max-md:h-[96dvh] max-md:max-h-[96dvh]">
        <form onSubmit={submit} noValidate className="flex flex-col gap-3">
          {edit && dirty ? <p className="text-xs font-semibold text-warn">● unsaved changes</p> : null}
          {saveError ? <p role="alert" className="rounded-xl border border-bad/40 bg-bad/10 p-3 text-sm text-t1">{saveError}</p> : null}
          <div className="grid grid-cols-[minmax(0,1fr)_8rem] gap-2.5">
            <TextField label="Name" required error={errors.name?.message} autoComplete="off" {...register("name")} />
            <TextField label="Auto-update, min" hint="0 = off" type="number" inputMode="numeric" min={0} error={errors.minutes?.message} {...register("minutes")} />
          </div>
          <TextField
            label="URL"
            required
            hint={edit ? "re-validated only when changed" : undefined}
            error={errors.url?.message}
            autoComplete="off"
            className="font-mono text-xs"
            {...register("url")}
          />
          {edit ? (
            <>
              <div className="flex items-center justify-between gap-3 rounded-xl border border-line bg-glass px-3 py-2">
                <div>
                  <p className="text-sm font-semibold text-t1">Enabled</p>
                  <p className="text-[11px] text-t3">auto + refresh all</p>
                </div>
                <Controller
                  control={control}
                  name="enabled"
                  render={({ field }) => <Toggle label="Enabled" checked={field.value} onCheckedChange={field.onChange} />}
                />
              </div>
              <FieldShell id="sub-form-profile" label="Default tuning profile for new nodes">
                {/* Controlled: the profiles arrive after the form opens, and the stored choice must still show once they do. */}
                <Controller
                  control={control}
                  name="default_profile_id"
                  render={({ field }) => (
                    <select id="sub-form-profile" {...field} className={SELECT_CLASS}>
                      <option value="">(global default)</option>
                      {profiles.data?.map((profile) => <option key={profile.id} value={String(profile.id)}>{profile.name}</option>)}
                    </select>
                  )}
                />
              </FieldShell>
            </>
          ) : null}

          <KeyValueRowsEditor name="headers" control={control} register={register} legend="Headers" item="Header" addLabel="Add header" keyPlaceholder="header" empty="No headers" />
          <p className="-mt-1.5 px-1 text-[11px] text-t3">Detailed device fingerprint placeholders are opt-in; the default sends only coarse OS data.</p>
          <KeyValueRowsEditor name="queries" control={control} register={register} legend="Query params" item="Query param" addLabel="Add param" keyPlaceholder="key" empty="No query params" />

          <div className="flex flex-wrap items-center gap-2">
            <Button disabled={preview.busy} onClick={() => void runPreview()}>{preview.busy ? "Previewing…" : "Preview request"}</Button>
            <Button disabled={dryRun.busy} onClick={() => void runDryRun()}>{dryRun.busy ? "Parsing… (up to 20 s)" : "Dry-run parse"}</Button>
            <span className="text-[11px] text-t3">preview doesn't fetch · dry-run does</span>
          </div>
          {preview.data ? (
            previewStale ? <p className="text-xs text-t3">Form changed since this run — run it again.</p> : <RequestPreview preview={preview.data} />
          ) : null}
          {preview.error ? <p role="alert" className="text-xs text-bad">{preview.error}</p> : null}
          {dryRun.data ? (
            dryRunStale ? (
              <p className="text-xs text-t3">Form changed since this run — run it again.</p>
            ) : (
              <DryRunResult result={dryRun.data} url={dryRun.target?.url ?? ""} />
            )
          ) : null}
          {dryRun.error ? <p role="alert" className="text-xs text-bad">{dryRun.error}</p> : null}

          <div className="sticky bottom-0 -mx-5 -mb-5 flex items-center justify-end gap-2 border-t border-line bg-solid px-5 py-3">
            <span className="mr-auto text-[11px] text-t3">Nothing is saved until {edit ? "Save" : "Add subscription"}</span>
            <Button onClick={() => void closeGuarded(dirty, onClose)}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={save.isPending}>{save.isPending ? "Saving…" : edit ? "Save" : "Add subscription"}</Button>
          </div>
        </form>
      </SheetContent>
    </Sheet>
  );
}
