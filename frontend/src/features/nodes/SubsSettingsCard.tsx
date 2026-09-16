import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, isNoAnswer, type Settings } from "../../api/client";
import {
  CONNECTION_BUSY, SETTINGS_BUSY, SETTINGS_CONNECTION_WRITE, SETTINGS_WRITE, invalidateRefused, isConnectionBusy, isSettingsBusy, saveRefusedMessage,
  useApiWrite, useConnectionBusy, useSettingsBusy,
} from "../../api/invalidation";
import { keys, queries } from "../../api/keys";
import { cardFallback } from "../../components/data/CardState";
import { CardHeader } from "../../components/data/CardHeader";
import { GlassCard } from "../../components/ui/GlassCard";
import { Toggle } from "../../components/ui/Toggle";
import { notifyError, notifyWarn } from "../../components/ui/Toaster";
import { confirmTunnelStart } from "../../lib/tunnelStart";
import { NO_ANSWER } from "../gateway/networkForm";

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

interface Flip {
  key: SubsSetting;
  on: boolean;
}

/**
 * G1, subscription half: two gateway settings, each saved on its own the moment it is flipped. tunneled_fetch re-applies
 * the live tunnel (a connection write, SETTINGS_CONNECTION_WRITE); subs_auto_switch does not (SETTINGS_WRITE). Any
 * settings write disables both switches, so the rollback and re-read below only ever reason about one write — and so
 * does any connection write, because PUT /settings takes the gateway's apply lock whatever it writes, and an Apply to
 * host can hold that lock for three minutes.
 */
export function SubsSettingsCard({ className }: { className?: string }) {
  const queryClient = useQueryClient();
  const settings = useQuery(queries.settings());   // read once; this card is its only writer on the screen
  const putSettings = useApiWrite("putSettings");
  const settingsBusy = useSettingsBusy();
  const connectionBusy = useConnectionBusy();

  // Optimistic: the switch moves at once. A failure puts back only the field it changed.
  const onMutate = ({ key, on }: Flip) => {
    const before = queryClient.getQueryData<Settings>(keys.settings)?.[key];
    queryClient.setQueryData<Settings>(keys.settings, (old) => (old ? { ...old, [key]: on } : old));
    return { key, before };
  };
  const revert = (context: { key: SubsSetting; before: boolean | undefined } | undefined) => {
    if (context?.before === undefined) return;
    const { key, before } = context;
    queryClient.setQueryData<Settings>(keys.settings, (old) => (old ? { ...old, [key]: before } : old));
  };
  const failed = (error: Error) => {
    // The rollback is a best guess from this write's own snapshot; re-read the gateway's actual value too.
    void queryClient.invalidateQueries({ queryKey: keys.settings });
    notifyError(error, "setting was not saved");
  };

  const tunneled = useMutation({
    mutationKey: SETTINGS_CONNECTION_WRITE,
    mutationFn: ({ key, on }: Flip) => putSettings({ [key]: on }),
    onMutate,
    onError: (error, _flip, context) => {
      revert(context);
      if (error instanceof ApiError && error.status === 502) {
        // The re-apply failed and the settings transaction rolled the change back: nothing was saved.
        notifyError(null, saveRefusedMessage(error));
        void invalidateRefused(queryClient, keys.settings);
      } else if (isNoAnswer(error)) {
        // No reply, but the gateway keeps working under its lock: the flip may well have committed. Say so
        // and re-read, rather than claiming it was not saved.
        notifyWarn(NO_ANSWER);
        void invalidateRefused(queryClient, keys.settings);
      } else {
        failed(error);
      }
    },
  });
  const autoSwitch = useMutation({
    mutationKey: SETTINGS_WRITE,
    mutationFn: ({ key, on }: Flip) => putSettings({ [key]: on }),
    onMutate,
    onError: (error, _flip, context) => {
      revert(context);
      failed(error);
    },
  });

  async function flip(key: SubsSetting, on: boolean) {
    if (key === "subs_auto_switch") {
      autoSwitch.mutate({ key, on });
      return;
    }
    // Re-applying starts an xray the operator stopped while a node is still selected: ask first.
    if (!(await confirmTunnelStart(queryClient))) return;
    if (isConnectionBusy(queryClient) || isSettingsBusy(queryClient)) {
      notifyError(null, isConnectionBusy(queryClient) ? CONNECTION_BUSY : SETTINGS_BUSY);
      return;
    }
    tunneled.mutate({ key, on });
  }

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
                disabled={settingsBusy || connectionBusy}
                onCheckedChange={(on) => void flip(row.key, on)}
              />
            </li>
          ))}
        </ul>
      )}
    </GlassCard>
  );
}
