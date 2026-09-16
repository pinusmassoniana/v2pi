import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Controller, useForm, useWatch } from "react-hook-form";
import { ApiError, isNoAnswer, type Settings } from "../../api/client";
import { SLOW_POLL_MS } from "../../api/cadence";
import {
  CONNECTION_BUSY, RESET_WRITE_KEY, SETTINGS_BUSY, isConnectionBusy, isSettingsBusy, saveRefusedMessage,
  settingsWriteKey, useApiWrite, useConnectionBusy, useSettingsBusy,
} from "../../api/invalidation";
import { keys, queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { useUnsavedGuard } from "../../app/guard";
import { AlertBanner } from "../../components/data/AlertBanner";
import { CardHeader } from "../../components/data/CardHeader";
import { cardFallback, staleNotice, type CardQuery } from "../../components/data/CardState";
import { Chip } from "../../components/data/Chip";
import { KeyValueRows } from "../../components/data/KeyValueRows";
import { confirm } from "../../components/confirm";
import { Button } from "../../components/ui/Button";
import { TextField } from "../../components/ui/Field";
import { GlassCard } from "../../components/ui/GlassCard";
import { Toggle } from "../../components/ui/Toggle";
import { notifyError, notifyOk, notifyWarn } from "../../components/ui/Toaster";
import { downloadText } from "../../lib/download";
import { fmtBytes, fmtUptimeCoarse } from "../../lib/format";
import { cn } from "../../lib/cn";
import { DESKTOP_QUERY, useMediaQuery } from "../../lib/media";
import { confirmTunnelStart, knownStatus, startsStoppedTunnel } from "../../lib/tunnelStart";
import { NO_ANSWER } from "../gateway/networkForm";
import { EditorSection } from "../tunnel/EditorSection";
import { CheckRow } from "./BackupsScreen";
import { FilePicker } from "./FilePicker";
import { ResultLine } from "./ResultLine";
import {
  DANGER_ZONE_HINT, EXPORT_INTRO, IMPORTED_MESSAGE, IMPORT_CONFIRM, IMPORT_NOTE, IMPORT_UNKNOWN_HINT, RESET_KEYS,
  SETTINGS_FILENAME, STOPPED_XRAY_CLAUSE, exportedMessage, exportedText, importChecks, resetConfirm, resetMessage,
  settingsFileTooLarge,
} from "./settingsFile";
import {
  STATS_NOTE, collectorOkLabel, collectorWarning, settingsToStatsForm, statsFormSchema, statsInvalidMessage,
  statsPatch, statsPatchReapplies, statsSavedMessage, type StatsFormValues,
} from "./statsForm";

const SAMPLE_HELPER = "the traffic socket's tick · plain save, no rebuild";
const PORT_HELPER = "1–65535 · 52345 and 10808 are taken by the gateway";
const STATS_FOOTER = "only the changed field is sent — the interval alone does not rebuild the tunnel";
const DIAGNOSTICS_HINT = "Uptime is the panel process, not the host and not the tunnel.";
const XRAY_STATES = new Set(["unavailable", "unknown"]);

/** P3. 30 s and never faster: each GET spawns `xray -version` with a 5 s timeout. */
export function SystemCard({ phone = false }: { phone?: boolean }) {
  const diagnostics = usePolledQuery(queries.diagnostics(), SLOW_POLL_MS);
  const data = diagnostics.data;
  const isState = data ? XRAY_STATES.has(data.xray_version) : false;
  return (
    <GlassCard aria-label="System">
      <CardHeader
        title="System"
        detail={phone ? "30 s" : "re-read every 30 s"}
        aside={<Button size="sm" disabled={diagnostics.isFetching} onClick={() => void diagnostics.refetch()}>Refresh</Button>}
      />
      {staleNotice([diagnostics], "diagnostics did not refresh")}
      {cardFallback([diagnostics], "diagnostics unavailable", "h-40") ?? (
        <>
          <KeyValueRows
            rows={[
              { key: "APP VERSION", value: data!.app_version },
              { key: "XRAY-CORE", value: <span className={cn("font-mono", isState && "text-t3")}>{data!.xray_version}</span>, sub: isState ? "the panel could not run `xray -version`" : undefined },
              { key: "PANEL UPTIME", value: fmtUptimeCoarse(data!.uptime_sec) },
              { key: "DATABASE", value: fmtBytes(data!.db_bytes) },
              { key: "DISK FREE", value: fmtBytes(data!.disk_free_bytes) },
              { key: "DISK TOTAL", value: fmtBytes(data!.disk_total_bytes) },
            ]}
          />
          <p className="mt-3 text-[11px] text-t3">{DIAGNOSTICS_HINT}</p>
          <p className="mt-1 truncate font-mono text-[11px] text-t3">{data!.db_path}</p>
        </>
      )}
    </GlassCard>
  );
}

/** G2. Only the changed fields are sent, which is what keeps a sample-interval-only save out of the tunnel's way. */
export function useStatsForm(settings: Settings | undefined) {
  const queryClient = useQueryClient();
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const baseline = useMemo(() => (settings ? settingsToStatsForm(settings) : { stats_enabled: true, stats_api_port: "", traffic_sample_ms: "" }), [settings]);
  const form = useForm<StatsFormValues>({ resolver: zodResolver(statsFormSchema), defaultValues: baseline, mode: "onChange" });
  const { control, formState, handleSubmit, reset } = form;
  // Follow the gateway while nothing is edited; once something is, never reseed under the operator's hands.
  useEffect(() => { if (!formState.isDirty) reset(baseline); }, [baseline, formState.isDirty, reset]);
  const putSettings = useApiWrite("putSettings");
  const settingsBusy = useSettingsBusy();
  const connectionBusy = useConnectionBusy();
  const values = useWatch({ control });
  const patch = statsPatch({ ...baseline, ...values } as StatsFormValues, baseline);

  useUnsavedGuard(formState.isDirty);

  const save = useMutation({
    mutationKey: settingsWriteKey(patch),
    mutationFn: ({ body }: { body: Partial<Settings>; active: boolean }) => putSettings(body),
    onSuccess: (saved, { body, active }) => {
      // Reset to what the gateway now holds, so later polls can update the form again.
      reset(settingsToStatsForm(saved));
      setResult({ ok: true, text: "saved" });
      notifyOk(statsSavedMessage(body, active));
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 502) {
        // One transaction: the failed re-apply rolled the whole patch back, so the card goes back too.
        reset(baseline);
        setResult({ ok: false, text: "not saved" });
        notifyError(null, saveRefusedMessage(error));
        void queryClient.invalidateQueries({ queryKey: keys.settings });
        return;
      }
      if (isNoAnswer(error)) {
        notifyWarn(NO_ANSWER);
        setResult({ ok: false, text: "no answer yet" });
        for (const key of [keys.settings, keys.status, keys.network]) void queryClient.invalidateQueries({ queryKey: key });
        return;
      }
      setResult({ ok: false, text: error instanceof ApiError ? error.message : "save failed" });
      notifyError(error, "traffic stats were not saved");
    },
  });

  const submit = handleSubmit(async (next) => {
    const body = statsPatch(next, baseline);
    if (Object.keys(body).length === 0) return;
    // Re-applying starts an xray the operator stopped while a node is still selected: ask first.
    if (statsPatchReapplies(body) && !(await confirmTunnelStart(queryClient))) return;
    if (isConnectionBusy(queryClient)) return void notifyError(null, CONNECTION_BUSY);
    if (isSettingsBusy(queryClient)) return void notifyError(null, SETTINGS_BUSY);
    // Read at send time, not at click time: with no selected node the gateway applies nothing.
    save.mutate({ body, active: (knownStatus(queryClient)?.active_node_id ?? null) !== null });
  });

  const errorCount = Object.keys(formState.errors).length;
  return {
    form, result, submit, patch, errorCount,
    dirty: formState.isDirty,
    saving: save.isPending,
    busy: settingsBusy || connectionBusy,
    discard: () => { reset({ ...baseline }); setResult(null); },
  };
}

export type StatsState = ReturnType<typeof useStatsForm>;

export function TrafficStatsFields({ stats }: { stats: StatsState }) {
  const { control, register, formState: { errors } } = stats.form;
  return (
    <>
      <div className="flex items-start gap-3">
        <div className="pt-0.5">
          <Controller
            control={control}
            name="stats_enabled"
            render={({ field }) => <Toggle label="Collect traffic stats" checked={field.value} disabled={stats.busy} onCheckedChange={field.onChange} />}
          />
        </div>
        <div className="min-w-0">
          <p className="text-sm font-semibold text-t1">Collect traffic stats</p>
          <p className="text-[11.5px] leading-relaxed text-t3">{STATS_NOTE}</p>
          <p className="mt-1 inline-flex rounded-full bg-glass-2 px-2 py-0.5 text-[10.5px] font-semibold text-t3">applies live when a node is connected</p>
        </div>
      </div>
      <TextField
        label="Sample interval" hint="(ms, 500–60000)" type="number" inputMode="numeric" disabled={stats.busy}
        error={errors.traffic_sample_ms?.message} {...register("traffic_sample_ms")}
      />
      <p className="-mt-1 text-[11px] text-t3">{SAMPLE_HELPER}</p>
      <TextField
        label="Xray API port" type="number" inputMode="numeric" disabled={stats.busy}
        error={errors.stats_api_port?.message} {...register("stats_api_port")}
      />
      <p className="-mt-1 text-[11px] text-t3">{PORT_HELPER}</p>
    </>
  );
}

export function CollectorHealth({ stats }: { stats: StatsState }) {
  // A plain reader of the key the System card polls: one owner, one cadence, no second timer.
  const diagnostics = useQuery(queries.diagnostics()).data;
  const port = useWatch({ control: stats.form.control, name: "stats_api_port" });
  const warning = collectorWarning(diagnostics, port);
  if (warning) return <AlertBanner tone="warn" title={warning.title} text={warning.text} />;
  const ok = collectorOkLabel(diagnostics);
  if (!ok) return null;
  return (
    <p className="text-[11px] text-t3">
      <span className="font-semibold text-ok">COLLECTOR OK</span> · {ok}
    </p>
  );
}

export function TrafficStatsCard({ stats, settings }: { stats: StatsState; settings: CardQuery }) {
  return (
    <GlassCard aria-label="Traffic stats">
      <CardHeader
        title="Traffic stats"
        detail="the live graph on Home"
        aside={stats.dirty ? <Chip tone="warn">● unsaved changes</Chip> : null}
      />
      {cardFallback([settings], "Settings did not load") ?? (
        <div className="flex flex-col gap-2.5">
          <TrafficStatsFields stats={stats} />
          <CollectorHealth stats={stats} />
          <div className="flex flex-wrap items-center gap-2 border-t border-line pt-3">
            <ResultLine
              className="min-w-0 flex-1"
              result={stats.errorCount ? { ok: false, text: statsInvalidMessage(stats.errorCount) } : stats.result}
            />
            <Button disabled={!stats.dirty || stats.busy} onClick={stats.discard}>Discard</Button>
            <Button variant="primary" disabled={!stats.dirty || stats.errorCount > 0 || stats.busy} onClick={() => void stats.submit()}>
              {stats.saving ? "Saving…" : "Save"}
            </Button>
          </div>
          <p className="text-[11px] text-t3">{STATS_FOOTER}</p>
        </div>
      )}
    </GlassCard>
  );
}

/** G6's file half. Export is client-only: it writes what the panel already holds, with no request at all. */
export function useSettingsFile(settings: Settings | undefined) {
  const queryClient = useQueryClient();
  const [picked, setPicked] = useState<{ file: File; text: string } | null>(null);
  const putSettings = useApiWrite("putSettings");
  const settingsBusy = useSettingsBusy();
  const connectionBusy = useConnectionBusy();
  const parsed = picked ? importChecks(picked.text) : null;

  const save = useMutation({
    mutationKey: settingsWriteKey(parsed?.patch ?? {}),
    mutationFn: (body: Partial<Settings>) => putSettings(body),
    onSuccess: () => { setPicked(null); notifyOk(IMPORTED_MESSAGE); },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 502) {
        notifyError(null, saveRefusedMessage(error, "not applied"));
        void queryClient.invalidateQueries({ queryKey: keys.settings });
        return;
      }
      if (isNoAnswer(error)) {
        notifyWarn(NO_ANSWER);
        for (const key of [keys.settings, keys.status, keys.network]) void queryClient.invalidateQueries({ queryKey: key });
        return;
      }
      notifyError(null, `not applied — ${error instanceof ApiError ? error.message : "the file was refused"}`);
    },
  });

  function exportFile() {
    if (!settings) return;
    downloadText(SETTINGS_FILENAME, exportedText(settings), "application/json");
    notifyOk(exportedMessage());
  }

  async function pick(file: File | null) {
    if (!file) return void setPicked(null);
    if (settingsFileTooLarge(file.size)) return void setPicked({ file, text: "" });
    setPicked({ file, text: await file.text() });
  }

  async function importFile() {
    const patch = parsed?.patch;
    if (!patch || save.isPending) return;
    const question = startsStoppedTunnel(queryClient) && (parsed?.reapplyKeys.length ?? 0) > 0
      ? `${IMPORT_CONFIRM}\n\n${STOPPED_XRAY_CLAUSE}`
      : IMPORT_CONFIRM;
    if (!(await confirm(question, { confirmLabel: "Import settings", danger: false }))) return;
    if (isConnectionBusy(queryClient)) return void notifyError(null, CONNECTION_BUSY);
    if (isSettingsBusy(queryClient)) return void notifyError(null, SETTINGS_BUSY);
    save.mutate(patch);
  }

  return {
    picked, parsed, pick, exportFile, importFile,
    tooLarge: picked ? settingsFileTooLarge(picked.file.size) : null,
    saving: save.isPending,
    busy: settingsBusy || connectionBusy,
  };
}

export type SettingsFileState = ReturnType<typeof useSettingsFile>;

export function SettingsFileBody({ file, settings }: { file: SettingsFileState; settings: Settings | undefined }) {
  const unknown = file.parsed?.unknown ?? [];
  return (
    <>
      <p className="text-[11.5px] leading-relaxed text-t2">{EXPORT_INTRO}</p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button disabled={!settings} onClick={file.exportFile}>Export settings</Button>
        <p className="min-w-0 text-[11px] text-t3">
          <span className="font-mono text-t2">{SETTINGS_FILENAME}</span> · 16 keys
        </p>
      </div>
      <div className="mt-3 border-t border-line pt-3">
        <FilePicker label="Choose file…" file={file.picked?.file ?? null} onPick={(picked) => void file.pick(picked)} disabled={file.busy} />
        {file.tooLarge ? (
          <AlertBanner tone="bad" className="mt-3" title={file.tooLarge.title} text={file.tooLarge.text} />
        ) : file.parsed ? (
          <>
            <ul className="mt-3 flex flex-col gap-1">
              {file.parsed.checks.map((check) => <CheckRow key={check.label} check={check} />)}
            </ul>
            {unknown.length ? <p className="mt-2 text-[11px] leading-relaxed text-t3">{IMPORT_UNKNOWN_HINT}</p> : null}
          </>
        ) : null}
        <div className="mt-3 flex justify-end">
          <Button variant="primary" disabled={!file.parsed?.patch || file.busy} onClick={() => void file.importFile()}>
            {file.saving ? "Importing…" : "Import settings"}
          </Button>
        </div>
      </div>
      <p className="mt-3 rounded-xl border border-line bg-glass px-3 py-2 text-[11.5px] leading-relaxed text-t2">{IMPORT_NOTE}</p>
    </>
  );
}

export function SettingsFileCard({ file, settings }: { file: SettingsFileState; settings: Settings | undefined }) {
  return (
    <GlassCard aria-label="Settings file">
      <CardHeader title="Settings file" detail="export / import" aside={<Chip plain>max 1 MB</Chip>} />
      <SettingsFileBody file={file} settings={settings} />
    </GlassCard>
  );
}

/** P5. `POST /settings/reset` always re-applies, so the question always names the rebuild. A 502 rolls all of it back. */
export function useResetSettings() {
  const queryClient = useQueryClient();
  const resetSettings = useApiWrite("resetSettings");
  const settingsBusy = useSettingsBusy();
  const connectionBusy = useConnectionBusy();

  const run = useMutation({
    mutationKey: RESET_WRITE_KEY,
    mutationFn: ({ active }: { active: boolean }) => resetSettings().then(() => active),
    onSuccess: (active) => notifyOk(resetMessage(active)),
    onError: (error) => {
      if (error instanceof ApiError && error.status === 502) {
        notifyError(null, saveRefusedMessage(error, "not reset"));
        void queryClient.invalidateQueries({ queryKey: keys.settings });
        return;
      }
      if (isNoAnswer(error)) {
        notifyWarn(NO_ANSWER);
        for (const key of [keys.settings, keys.status, keys.network]) void queryClient.invalidateQueries({ queryKey: key });
        return;
      }
      notifyError(error, "reset failed");
    },
  });

  async function reset() {
    if (!(await confirm(resetConfirm(startsStoppedTunnel(queryClient)), { confirmLabel: "Reset settings" }))) return;
    if (isConnectionBusy(queryClient)) return void notifyError(null, CONNECTION_BUSY);
    if (isSettingsBusy(queryClient)) return void notifyError(null, SETTINGS_BUSY);
    run.mutate({ active: (knownStatus(queryClient)?.active_node_id ?? null) !== null });
  }

  return { reset, resetting: run.isPending, busy: settingsBusy || connectionBusy };
}

export function DangerZoneBody({ danger, condensed = false }: { danger: ReturnType<typeof useResetSettings>; condensed?: boolean }) {
  const chips = condensed ? RESET_KEYS.slice(0, 9) : RESET_KEYS;
  return (
    <>
      <p className="text-sm font-semibold text-t1">Reset panel settings to defaults</p>
      <p className="mt-1 text-[11.5px] leading-relaxed text-t3">{DANGER_ZONE_HINT}</p>
      <ul className="mt-3 flex flex-wrap gap-1">
        {chips.map(([key, value]) => (
          <li key={key} className="rounded-full border border-line bg-glass px-2 py-0.5 font-mono text-[10.5px] text-t3">
            {key} {key === "health_probe_url" ? "…ipify.org" : value}
          </li>
        ))}
      </ul>
      <div className="mt-3 flex justify-end border-t border-line pt-3">
        <Button variant="danger" disabled={danger.busy} onClick={() => void danger.reset()}>
          {danger.resetting ? "Resetting…" : "Reset settings"}
        </Button>
      </div>
    </>
  );
}

export function DangerZoneCard({ danger }: { danger: ReturnType<typeof useResetSettings> }) {
  return (
    <GlassCard aria-label="Danger zone" className="border-bad/40 bg-bad/5">
      <CardHeader title="Danger zone" detail="16 settings" />
      <DangerZoneBody danger={danger} />
    </GlassCard>
  );
}

function PanelPhone({ stats, file, danger, settings }: {
  stats: StatsState; file: SettingsFileState; danger: ReturnType<typeof useResetSettings>; settings: CardQuery & { data: Settings | undefined };
}) {
  const [open, setOpen] = useState<"stats" | "file" | "danger" | null>("stats");
  const section = (id: "stats" | "file" | "danger") => ({
    collapsible: true as const,
    open: open === id,
    onToggle: () => setOpen((current) => (current === id ? null : id)),
  });
  return (
    <div className="flex flex-col gap-3">
      <SystemCard phone />
      <GlassCard>
        <EditorSection
          title="Traffic stats"
          note="the live graph on Home"
          {...section("stats")}
          aside={stats.dirty ? <Chip tone="warn">● unsaved</Chip> : null}
        >
          {cardFallback([settings], "Settings did not load") ?? (
            <>
              <TrafficStatsFields stats={stats} />
              <CollectorHealth stats={stats} />
            </>
          )}
        </EditorSection>
        <EditorSection title="Settings file" note="export / import" {...section("file")}>
          <SettingsFileBody file={file} settings={settings.data} />
        </EditorSection>
        <EditorSection title="Danger zone" note="16 settings" {...section("danger")}>
          <DangerZoneBody danger={danger} condensed />
        </EditorSection>
      </GlassCard>
      {stats.dirty ? (
        <div className="glass sticky bottom-24 z-20 flex flex-col gap-2 bg-solid p-2.5">
          <ResultLine
            result={stats.errorCount ? { ok: false, text: statsInvalidMessage(stats.errorCount) } : stats.result}
          />
          <p className="text-[11px] text-t3">{STATS_FOOTER}</p>
          <div className="flex gap-2">
            <Button className="flex-1" disabled={stats.busy} onClick={stats.discard}>Discard</Button>
            <Button variant="primary" className="flex-1" disabled={stats.errorCount > 0 || stats.busy} onClick={() => void stats.submit()}>
              {stats.saving ? "Saving…" : "Save"}
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

/** System › Panel (P3, P5, G2 and G6's file half). It owns `settings` and `diagnostics` at the slow cadence. */
export function Panel() {
  const desktop = useMediaQuery(DESKTOP_QUERY);
  const settings = usePolledQuery(queries.settings(), SLOW_POLL_MS);
  const stats = useStatsForm(settings.data);
  const file = useSettingsFile(settings.data);
  const danger = useResetSettings();
  if (!desktop) return <PanelPhone stats={stats} file={file} danger={danger} settings={settings} />;
  return (
    <div className="grid gap-3 md:grid-cols-[7fr_5fr] md:items-start">
      <div className="flex flex-col gap-3">
        <TrafficStatsCard stats={stats} settings={settings} />
        <SettingsFileCard file={file} settings={settings.data} />
      </div>
      <div className="flex flex-col gap-3">
        <SystemCard />
        <DangerZoneCard danger={danger} />
      </div>
    </div>
  );
}
