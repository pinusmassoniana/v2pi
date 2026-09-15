import { zodResolver } from "@hookform/resolvers/zod";
import { useIsMutating, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, type ReactNode } from "react";
import { Controller, useForm, useWatch } from "react-hook-form";
import type { Settings } from "../../api/client";
import { SETTINGS_WRITE, useApiWrite } from "../../api/invalidation";
import { keys, queries } from "../../api/keys";
import { useUnsavedGuard } from "../../app/guard";
import { CardHeader } from "../../components/data/CardHeader";
import { Button } from "../../components/ui/Button";
import { TextField } from "../../components/ui/Field";
import { GlassCard } from "../../components/ui/GlassCard";
import { ErrorState, Skeleton } from "../../components/ui/States";
import { Toggle } from "../../components/ui/Toggle";
import { notifyError, notifyOk } from "../../components/ui/Toaster";
import { cn } from "../../lib/cn";
import {
  ACTIVE_CHECK_EXPLAINER, DEMOTION_NOTE, MAX_PROBE_URL, NO_REAPPLY_NOTE, SAVED_MESSAGE, healthFormSchema, healthFormToPatch, settingsToHealthForm,
  type HealthFormValues,
} from "./healthForm";
import { HealthStateStrip } from "./HealthStateStrip";

type HealthPatch = ReturnType<typeof healthFormToPatch>;

function SwitchRow({ title, text, children, className }: { title: string; text: string; children: ReactNode; className?: string }) {
  return (
    <div className={cn("flex items-center justify-between gap-3 rounded-xl border border-line bg-glass px-3 py-2.5", className)}>
      <div className="min-w-0">
        <p className="text-sm font-semibold text-t1">{title}</p>
        <p className="text-[11.5px] text-t3">{text}</p>
      </div>
      {children}
    </div>
  );
}

function Help({ children }: { children: ReactNode }) {
  return <p className="-mt-1 text-[11px] text-t3">{children}</p>;
}

/**
 * G3 and G4: one form over both cards — a plain container, not a <form>: radix switches inside a form add hidden
 * checkboxes of their own. It follows the gateway's settings wherever the operator has not typed
 * (keepDirtyValues), and a save sends only the settings that changed — a partial PUT /settings that re-applies nothing.
 */
function HealthForm({ settings }: { settings: Settings }) {
  const queryClient = useQueryClient();
  const baseline = useMemo(() => settingsToHealthForm(settings), [settings]);
  const { register, control, handleSubmit, reset, formState: { errors, isDirty } } = useForm<HealthFormValues>({
    resolver: zodResolver(healthFormSchema),
    defaultValues: baseline,
    values: baseline,
    resetOptions: { keepDirtyValues: true },
    mode: "onChange",
  });
  useUnsavedGuard(isDirty);
  const [master, sweep] = useWatch({ control, name: ["health_enabled", "health_sweep_enabled"] });
  const putSettings = useApiWrite("putSettings");
  // One settings write at a time, shared with every other settings card (SETTINGS_WRITE).
  const saving = useIsMutating({ mutationKey: SETTINGS_WRITE }) > 0;
  const save = useMutation({
    mutationKey: SETTINGS_WRITE,
    mutationFn: (patch: HealthPatch) => putSettings(patch),
    onSuccess: () => notifyOk(SAVED_MESSAGE),
    onError: (error) => {
      // What was stored is not known after a failure: read the settings again.
      void queryClient.invalidateQueries({ queryKey: keys.settings });
      notifyError(error, "health settings were not saved");
    },
  });

  const submit = handleSubmit((values) => {
    const patch = healthFormToPatch(values, baseline);
    if (Object.keys(patch).length === 0) return;
    save.mutate(patch, {
      onSuccess: (saved) => {
        queryClient.setQueryData(keys.settings, saved);
        reset(settingsToHealthForm(saved));
      },
    });
  });
  const invalid = Object.keys(errors).length > 0;

  return (
    <div className="flex flex-col gap-3">
      <div className="grid items-start gap-3 md:grid-cols-2">
        <GlassCard aria-label="Health monitoring" className="flex flex-col gap-3">
          <CardHeader title="Health monitoring" className="mb-0" />
          <SwitchRow title="Health checks" text="master switch — off stops both checks below">
            <Controller control={control} name="health_enabled" render={({ field }) => <Toggle label="Health checks" checked={field.value} onCheckedChange={field.onChange} />} />
          </SwitchRow>
          <SwitchRow title="Server sweep" text="TCP + direct HTTPS across the whole pool" className={cn("ml-3", !master && "opacity-60")}>
            <Controller
              control={control}
              name="health_sweep_enabled"
              render={({ field }) => <Toggle label="Server sweep" checked={field.value} disabled={!master} onCheckedChange={field.onChange} />}
            />
          </SwitchRow>
          <div className="ml-3 flex flex-col gap-2.5">
            <TextField
              label="Server sweep interval"
              hint="minutes"
              type="number"
              inputMode="numeric"
              min={1}
              disabled={!master || !sweep}
              error={errors.sweep_minutes?.message}
              {...register("sweep_minutes")}
            />
            <Help>stored as seconds · floor 1 min · default 30</Help>
          </div>
          <TextField
            label="Active-server check interval"
            hint="seconds"
            type="number"
            inputMode="numeric"
            min={10}
            disabled={!master}
            error={errors.active_seconds?.message}
            {...register("active_seconds")}
          />
          <Help>floor 10 s · default 60</Help>
          <TextField label="Probe URL" hint={`≤ ${MAX_PROBE_URL} characters`} type="url" autoComplete="off" className="font-mono text-xs" error={errors.health_probe_url?.message} {...register("health_probe_url")} />
          <p className="text-xs leading-relaxed text-t2">{ACTIVE_CHECK_EXPLAINER}</p>
        </GlassCard>

        <GlassCard aria-label="Auto-failover" className="flex flex-col gap-3">
          <CardHeader title="Auto-failover" className="mb-0" />
          <SwitchRow title="Auto-failover" text="switch to a healthy standby when the active node fails">
            <Controller control={control} name="failover_enabled" render={({ field }) => <Toggle label="Auto-failover" checked={field.value} onCheckedChange={field.onChange} />} />
          </SwitchRow>
          {!master ? <p className="text-xs text-warn">health checks are off — failover can never fire</p> : null}
          <TextField label="Hysteresis" hint="checks" type="number" inputMode="numeric" min={1} error={errors.hysteresis?.message} {...register("hysteresis")} />
          <Help>consecutive failed real checks before switching</Help>
          <TextField label="Cooldown" hint="seconds" type="number" inputMode="numeric" min={0} error={errors.cooldown_seconds?.message} {...register("cooldown_seconds")} />
          <Help>minimum time between automatic switches</Help>
          <p className="flex flex-wrap items-center gap-2 text-xs text-t2">
            {DEMOTION_NOTE}
            <span className="rounded-full bg-glass-2 px-2 py-0.5 text-[10.5px] font-semibold text-t3">fixed · not a setting</span>
          </p>
        </GlassCard>
      </div>

      <div className="glass flex items-center gap-3 bg-solid p-2.5 max-md:sticky max-md:bottom-24 max-md:z-20">
        <p className="min-w-0 flex-1 text-[11.5px] text-t3">{isDirty ? <span className="font-semibold text-warn">● unsaved changes · </span> : null}{NO_REAPPLY_NOTE}</p>
        <Button variant="primary" disabled={!isDirty || invalid || saving} onClick={() => void submit()}>{save.isPending ? "Saving…" : "Save"}</Button>
      </div>
    </div>
  );
}

/** Tunnel › Health & failover (G3, G4): settings read once (refetched after writes), the state strip from the status poll. */
export function Health() {
  const settings = useQuery(queries.settings());
  return (
    <div className="flex flex-col gap-3">
      <HealthStateStrip />
      {settings.data ? (
        <HealthForm settings={settings.data} />
      ) : settings.isError ? (
        <ErrorState message="Health settings did not load" onRetry={() => void settings.refetch()} />
      ) : (
        <div aria-busy className="grid gap-3 md:grid-cols-2">
          <Skeleton className="h-96" />
          <Skeleton className="h-96" />
        </div>
      )}
    </div>
  );
}
