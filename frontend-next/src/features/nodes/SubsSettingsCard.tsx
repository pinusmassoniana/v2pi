import { useIsMutating, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Settings } from "../../api/client";
import { SETTINGS_WRITE, useApiWrite } from "../../api/invalidation";
import { keys, queries } from "../../api/keys";
import { cardFallback } from "../../components/data/CardState";
import { CardHeader } from "../../components/data/CardHeader";
import { GlassCard } from "../../components/ui/GlassCard";
import { Toggle } from "../../components/ui/Toggle";
import { notifyError } from "../../components/ui/Toaster";

type SubsSetting = "tunneled_fetch" | "subs_auto_switch";

const ROWS: readonly { key: SubsSetting; label: string; text: string; note: string }[] = [
  {
    key: "tunneled_fetch",
    label: "Fetch subscriptions through the tunnel",
    text: "Refresh and dry-run go through the active node.",
    note: "applies live",
  },
  {
    key: "subs_auto_switch",
    label: "Subscription auto-switch",
    text: "Let a scheduled subscription refresh replace the active node on its own, with no operator action, when the one you're on drops out of the feed.",
    note: "never switches to weaker security; off keeps switching manual",
  },
];

/** G1, subscription half: two gateway settings, each saved on its own the moment it is flipped. */
export function SubsSettingsCard({ className }: { className?: string }) {
  const queryClient = useQueryClient();
  const settings = useQuery(queries.settings());   // read once; this card is its only writer on the screen
  const putSettings = useApiWrite("putSettings");
  // Both rows share SETTINGS_WRITE: a save in flight disables every switch on the card, so a second click (the same
  // row twice, or the other row) never overlaps the first — the rollback/invalidate logic below only has to reason
  // about one write at a time.
  const toggle = useMutation({
    mutationKey: SETTINGS_WRITE,
    mutationFn: ({ key, on }: { key: SubsSetting; on: boolean }) => putSettings({ [key]: on }),
    // Optimistic: the switch moves at once. A failure puts back only the field it changed, so a second switch
    // flipped meanwhile keeps its own new value.
    onMutate: ({ key, on }) => {
      const before = queryClient.getQueryData<Settings>(keys.settings)?.[key];
      queryClient.setQueryData<Settings>(keys.settings, (old) => (old ? { ...old, [key]: on } : old));
      return { key, before };
    },
    onError: (error, _variables, context) => {
      if (context?.before !== undefined) {
        const { key, before } = context;
        queryClient.setQueryData<Settings>(keys.settings, (old) => (old ? { ...old, [key]: before } : old));
      }
      // The rollback is a best guess from this write's own snapshot; re-read the gateway's actual value too.
      void queryClient.invalidateQueries({ queryKey: keys.settings });
      notifyError(error, "setting was not saved");
    },
  });
  // One write at a time: disables both switches so a click can't land while another row's save is still out,
  // which is what let a rollback or a stale refetch show the wrong value (see the mutation's own comment).
  const saving = useIsMutating({ mutationKey: SETTINGS_WRITE }) > 0;

  const fallback = cardFallback([settings], "Subscription settings did not load", "h-24");
  return (
    <GlassCard aria-label="Subscription fetching" className={className}>
      <CardHeader title="Subscription fetching" detail="global settings" />
      {fallback ?? (
        <ul className="flex flex-col divide-y divide-line">
          {ROWS.map((row) => (
            <li key={row.key} className="flex items-start gap-3 py-2.5 first:pt-0 last:pb-0">
              <div className="min-w-0 flex-1">
                <p className="text-sm font-semibold text-t1">{row.label}</p>
                <p className="text-xs text-t2">{row.text}</p>
                <p className="text-[11px] text-t3">{row.note}</p>
              </div>
              <Toggle
                label={row.label}
                checked={settings.data?.[row.key] ?? false}
                disabled={saving}
                onCheckedChange={(on) => toggle.mutate({ key: row.key, on })}
              />
            </li>
          ))}
        </ul>
      )}
    </GlassCard>
  );
}
