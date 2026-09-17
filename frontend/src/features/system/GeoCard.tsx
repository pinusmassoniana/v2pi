import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Undo2 } from "lucide-react";
import type { Geo, GeoFile, GeoUpdate } from "../../api/client";
import { CONNECTION_WRITE, useApiWrite, useConnectionBusy } from "../../api/invalidation";
import { queries } from "../../api/keys";
import { useNow } from "../../components/data/Ago";
import { CardHeader } from "../../components/data/CardHeader";
import { cardFallback, type CardQuery } from "../../components/data/CardState";
import { Chip } from "../../components/data/Chip";
import { confirm } from "../../components/confirm";
import { Button } from "../../components/ui/Button";
import { GlassCard } from "../../components/ui/GlassCard";
import { notifyError, notifyOk } from "../../components/ui/Toaster";
import { agoLabel } from "../../lib/dashboard";
import { fmtBytes } from "../../lib/format";

/** A3. The two datasets, in the order the card shows them. */
export const DATASETS = [
  { key: "stock", title: "Stock lists", detail: "geoip: / geosite: — v2fly, what the image ships" },
  { key: "ru", title: "RU lists", detail: "ext:geosite_ru.dat: — runetfreedom, blocked in Russia" },
] as const;

export const GEO_NOTE =
  "Replacing a list restarts the tunnel, so everyone behind the gateway drops for a second or two. "
  + "The new files are checked against the running config first, and the previous ones are kept.";

export const UPDATE_CONFIRM =
  "Update this geo data now? The tunnel restarts once the new files pass their check — devices drop briefly.";
export const REVERT_CONFIRM = "Put the previous copy of this geo data back? The tunnel restarts.";

/** What a finished update says: what changed, or why nothing did. */
export function geoOutcome(result: GeoUpdate): string {
  if (!result.ok) return result.error || "the geo data was not replaced";
  const files = result.files.length ? result.files.join(", ") : "nothing";
  return `${result.dataset} geo data updated (${files})${result.reloaded ? " · tunnel restarted" : ""}`;
}

function DatasetRow({ dataset, files, busy, onUpdate, onRevert }: {
  dataset: (typeof DATASETS)[number];
  files: GeoFile[];
  busy: boolean;
  onUpdate: () => void;
  onRevert: () => void;
}) {
  const present = files.filter((file) => file.present);
  const newest = present.reduce<number | null>(
    (latest, file) => (file.updated_at && (latest === null || file.updated_at > latest) ? file.updated_at : latest), null);
  const bytes = present.reduce((sum, file) => sum + file.bytes, 0);
  const revertable = files.some((file) => file.has_previous);
  const nowSec = Math.floor(useNow() / 1000);
  return (
    <div data-geo-dataset={dataset.key} className="border-t border-line py-2.5 first:border-t-0 first:pt-0">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-[13px] font-bold text-t1">{dataset.title}</h3>
        {present.length === 0 ? <Chip tone="warn">not installed</Chip> : <Chip plain tone="neutral">{fmtBytes(bytes)}</Chip>}
        <span className="ml-auto text-[11px] text-t3">
          {newest ? `updated ${agoLabel(newest, nowSec)}` : "never updated here"}
        </span>
      </div>
      <p className="mt-0.5 font-mono text-[11px] text-t3">{dataset.detail}</p>
      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
        <Button size="sm" disabled={busy} onClick={onUpdate}>
          <RefreshCw size={13} aria-hidden />
          Update {dataset.title.toLowerCase()}
        </Button>
        {revertable ? (
          <Button size="sm" variant="ghost" disabled={busy} onClick={onRevert}>
            <Undo2 size={13} aria-hidden />
            Revert
          </Button>
        ) : null}
      </div>
    </div>
  );
}

/**
 * A3: the routing data files the gateway actually loads, and the two buttons that replace them.
 * Manual on purpose — a swap only takes effect when xray reloads, and that is a drop for every
 * device behind the gateway, so it never happens unasked.
 */
export function GeoCard() {
  const queryClient = useQueryClient();
  const geo = useQuery(queries.geo());      // read once; nothing polls files that only change here
  const updateGeo = useApiWrite("updateGeo");
  const revertGeo = useApiWrite("revertGeo");
  const connectionBusy = useConnectionBusy();

  const run = useMutation({
    // A geo swap restarts the tunnel, so it takes the same lock every other connection write does.
    mutationKey: CONNECTION_WRITE,
    mutationFn: ({ dataset, revert }: { dataset: string; revert: boolean }) =>
      (revert ? revertGeo(dataset) : updateGeo(dataset)),
    onSuccess: (result) => {
      if (result.ok) notifyOk(geoOutcome(result));
      else notifyError(null, geoOutcome(result));
      queryClient.setQueryData<Geo>(queries.geo().queryKey, result.geo);
    },
    onError: (error) => notifyError(error, "the geo data was not replaced"),
  });

  const busy = run.isPending || connectionBusy;
  async function act(dataset: string, revert: boolean) {
    if (!(await confirm(revert ? REVERT_CONFIRM : UPDATE_CONFIRM, { confirmLabel: revert ? "Revert" : "Update" }))) return;
    run.mutate({ dataset, revert });
  }

  return (
    <GlassCard aria-label="Geo data">
      <CardHeader title="Geo data" detail="routing lists" aside={<Chip plain>manual</Chip>} />
      {cardFallback([geo as CardQuery], "geo data unavailable", "h-40") ?? (
        <>
          <div className="flex flex-col">
            {DATASETS.map((dataset) => (
              <DatasetRow
                key={dataset.key}
                dataset={dataset}
                files={geo.data!.files.filter((file) => file.dataset === dataset.key)}
                busy={busy}
                onUpdate={() => void act(dataset.key, false)}
                onRevert={() => void act(dataset.key, true)}
              />
            ))}
          </div>
          <p className="mt-2 text-[11px] leading-relaxed text-t3">{GEO_NOTE}</p>
          <p className="mt-1 truncate font-mono text-[11px] text-t3">
            {geo.data!.asset_dir} · {fmtBytes(geo.data!.disk_free_bytes)} free
          </p>
        </>
      )}
    </GlassCard>
  );
}
