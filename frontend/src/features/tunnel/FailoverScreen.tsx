import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { FailoverState, Settings } from "../../api/client";
import { serverNow } from "../../api/clock";
import { SLOW_POLL_MS } from "../../api/cadence";
import { SETTINGS_WRITE, useApiWrite, useConnectionBusy, useSettingsBusy } from "../../api/invalidation";
import { keys, queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { useUnsavedGuard } from "../../app/guard";
import { CardHeader } from "../../components/data/CardHeader";
import { cardFallback } from "../../components/data/CardState";
import { Button } from "../../components/ui/Button";
import { TextField } from "../../components/ui/Field";
import { GlassCard } from "../../components/ui/GlassCard";
import { Pill } from "../../components/ui/Pill";
import { Select } from "../../components/ui/Select";
import { notifyError, notifyOk } from "../../components/ui/Toaster";
import { failoverRows, fmtHealthy, moved, nodeSource, type FailoverRow } from "./failoverList";

/** The list itself. With `onMove`/`onRemove` it is the editor; without, the read-only Overview card body. */
export function FailoverListView({ rows, failbackMin, onMove, onRemove, busy }: {
  rows: FailoverRow[]; failbackMin?: number; busy?: boolean;
  onMove?: (id: number, delta: number) => void; onRemove?: (id: number) => void;
}) {
  return (
    <ol aria-label="Failover order" className="flex flex-col gap-1.5">
      {rows.map((row) => (
        <li key={row.id} className="flex items-center gap-3 rounded-xl border border-line bg-glass px-3 py-2">
          <span className="w-6 shrink-0 text-center font-mono text-sm font-semibold text-t2">{row.rank}</span>
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-semibold text-t1">
              {row.name}
              {row.active ? <span className="ml-2 rounded-full bg-glass-2 px-2 py-0.5 text-2xs font-semibold text-t2">connected</span> : null}
            </p>
            <p className="truncate text-caption text-t3">
              {row.source}
              {row.healthyFor !== null ? ` · healthy ${fmtHealthy(row.healthyFor)}` : ""}
              {row.healthyFor !== null && failbackMin !== undefined && !row.active
                ? row.healthyFor >= failbackMin * 60 ? " · ready to switch back" : ` · switch back after ${failbackMin} min`
                : ""}
            </p>
          </div>
          <Pill tone={row.tone} dot>{row.state}</Pill>
          {onMove && onRemove ? (
            <div className="flex shrink-0 gap-1">
              <Button size="icon" variant="ghost" aria-label={`Move ${row.name} up`} disabled={busy || row.rank === 1} onClick={() => onMove(row.id, -1)}>↑</Button>
              <Button size="icon" variant="ghost" aria-label={`Move ${row.name} down`} disabled={busy || row.rank === rows.length} onClick={() => onMove(row.id, 1)}>↓</Button>
              <Button size="icon" variant="ghost" aria-label={`Remove ${row.name}`} disabled={busy} onClick={() => onRemove(row.id)}>✕</Button>
            </div>
          ) : null}
        </li>
      ))}
    </ol>
  );
}

const FIELDS = [
  { key: "health_hysteresis", label: "Failed checks before switching", hint: "checks", low: 1, high: 1000 },
  { key: "failback_minutes", label: "Healthy time before switching back", hint: "minutes", low: 1, high: 1440 },
  { key: "failover_real_interval", label: "Check every node every", hint: "seconds", low: 10, high: 3600 },
  { key: "failover_probe_batch", label: "Nodes checked at once", hint: "per 20 s", low: 1, high: 20 },
] as const satisfies readonly { key: keyof Settings; label: string; hint: string; low: number; high: number }[];

type FieldKey = (typeof FIELDS)[number]["key"];
type Draft = Record<FieldKey, string>;

function draftOf(settings: Settings): Draft {
  return Object.fromEntries(FIELDS.map((f) => [f.key, String(settings[f.key])])) as Draft;
}

function issue(f: (typeof FIELDS)[number], value: string): string | undefined {
  const n = Number(value.trim());
  return /^\d+$/.test(value.trim()) && n >= f.low && n <= f.high ? undefined : `${f.low}–${f.high}`;
}

function RulesCard({ settings }: { settings: Settings }) {
  const queryClient = useQueryClient();
  // Only what the operator typed is held here; every other field follows the gateway's settings.
  const [edits, setEdits] = useState<Partial<Draft>>({});
  const baseline = draftOf(settings);
  const draft: Draft = { ...baseline, ...edits };
  const dirty = FIELDS.some((f) => draft[f.key].trim() !== baseline[f.key]);
  useUnsavedGuard(dirty);
  const putSettings = useApiWrite("putSettings");
  const settingsBusy = useSettingsBusy();
  const connectionBusy = useConnectionBusy();
  const busy = settingsBusy || connectionBusy;
  const save = useMutation({
    mutationKey: SETTINGS_WRITE,
    mutationFn: (patch: Partial<Settings>) => putSettings(patch),
    onSuccess: (saved) => { queryClient.setQueryData(keys.settings, saved); setEdits({}); notifyOk("Failover settings saved"); },
    onError: (error) => { void queryClient.invalidateQueries({ queryKey: keys.settings }); notifyError(error, "failover settings were not saved"); },
  });
  const invalid = FIELDS.some((f) => issue(f, draft[f.key]));
  const submit = () => {
    const patch: Partial<Settings> = {};
    for (const f of FIELDS) if (draft[f.key].trim() !== baseline[f.key]) patch[f.key] = Number(draft[f.key]);
    save.mutate(patch);
  };
  return (
    <GlassCard aria-label="Switching rules" className="flex flex-col gap-3">
      <CardHeader title="Switching rules" className="mb-0" />
      {FIELDS.map((f) => (
        <TextField key={f.key} label={f.label} hint={f.hint} type="number" inputMode="numeric" min={f.low} max={f.high}
          value={draft[f.key]} error={issue(f, draft[f.key])} onChange={(e) => setEdits({ ...edits, [f.key]: e.target.value })} />
      ))}
      <p className="text-xs leading-relaxed text-t2">
        Failed checks is shared with Tunnel › Health & failover. Each check starts a separate xray on the gateway, so the live connection is untouched.
      </p>
      <div className="flex items-center justify-end gap-2">
        {dirty ? <span className="text-caption font-semibold text-warn">unsaved changes</span> : null}
        <Button variant="primary" disabled={!dirty || invalid || busy} onClick={submit}>{save.isPending ? "Saving…" : "Save"}</Button>
      </div>
    </GlassCard>
  );
}

/** Tunnel › Failover list: the ordered nodes auto-failover moves along, and the rules for when it moves. */
export function Failover() {
  const queryClient = useQueryClient();
  const status = useQuery(queries.status());
  const failover = usePolledQuery(queries.failover(), SLOW_POLL_MS);
  const nodes = usePolledQuery(queries.nodes(), SLOW_POLL_MS);
  const health = usePolledQuery(queries.nodeHealth(), SLOW_POLL_MS);
  const subs = usePolledQuery(queries.subs(), SLOW_POLL_MS);
  const settings = useQuery(queries.settings());
  const [pick, setPick] = useState("");
  const putOrder = useApiWrite("putFailoverOrder");
  const write = useMutation({
    mutationFn: (order: number[]) => putOrder(order),
    onSuccess: (saved) => queryClient.setQueryData<FailoverState>(keys.failover, saved),
    onError: (error) => { void queryClient.invalidateQueries({ queryKey: keys.failover }); notifyError(error, "the failover list was not saved"); },
  });

  const fallback = cardFallback([failover, nodes, health, subs], "The failover list did not load", "h-44");
  const order = failover.data?.order ?? [];
  const rows = failover.data
    ? failoverRows(failover.data, nodes.data ?? [], health.data ?? [], subs.data ?? [], status.data?.active_node_id ?? null, serverNow())
    : [];
  const candidates = (nodes.data ?? []).filter((n) => !order.includes(n.id));

  return (
    <div className="grid items-start gap-3 md:grid-cols-[3fr_2fr]">
      <GlassCard aria-label="Failover list" className="flex flex-col gap-3">
        <CardHeader title="Failover list" aside={<span className="text-caption text-t3">{rows.length} node{rows.length === 1 ? "" : "s"}</span>} className="mb-0" />
        {fallback ?? (
          <>
            {rows.length === 0 ? (
              <p className="text-sm text-t3">Empty — auto-failover picks the healthiest node from all servers. Add nodes to make it follow your order instead.</p>
            ) : (
              <FailoverListView rows={rows} failbackMin={settings.data?.failback_minutes} busy={write.isPending}
                onMove={(id, delta) => write.mutate(moved(order, id, delta))}
                onRemove={(id) => write.mutate(order.filter((n) => n !== id))} />
            )}
            <div className="flex gap-2">
              <Select aria-label="Node to add" className="min-w-0 flex-1" value={pick} onChange={(e) => setPick(e.target.value)} disabled={candidates.length === 0}>
                <option value="">{candidates.length === 0 ? "every node is on the list" : "Add a node…"}</option>
                {candidates.map((n) => <option key={n.id} value={n.id}>{n.name} · {nodeSource(n, subs.data ?? [])}</option>)}
              </Select>
              <Button disabled={!pick || write.isPending} onClick={() => { write.mutate([...order, Number(pick)]); setPick(""); }}>Add</Button>
            </div>
            <p className="text-xs leading-relaxed text-t2">
              When the connected node fails, the service moves to the first node below it that is up — skipping any that are down. Once a higher node has been healthy for the set time, it moves back. Nodes not on the list are never used while it has entries; if every listed node is down, it stays put.
            </p>
          </>
        )}
      </GlassCard>
      {settings.data ? <RulesCard settings={settings.data} /> : cardFallback([settings], "Failover settings did not load", "h-72")}
    </div>
  );
}
