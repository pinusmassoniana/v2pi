import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, isNoAnswer, type Network, type Settings, type Status } from "../../api/client";
import {
  CONNECTION_BUSY, SETTINGS_BUSY, SETTINGS_CONNECTION_WRITE, invalidateRefused, isConnectionBusy, isSettingsBusy, saveRefusedMessage, useApiWrite,
  useConnectionBusy, useSettingsBusy,
} from "../../api/invalidation";
import { keys, queries } from "../../api/keys";
import { cardFallback } from "../../components/data/CardState";
import { Toggle } from "../../components/ui/Toggle";
import { notifyError, notifyOk, notifyWarn } from "../../components/ui/Toaster";
import { confirmTunnelStart } from "../../lib/tunnelStart";
import { NO_ANSWER } from "./networkForm";

interface Flip {
  on: boolean;
  /** A node was active when the switch was flipped, so the gateway re-applies the tunnel now. */
  active: boolean;
}

/** G1's toast once the gateway took the change. */
export function dnsSavedMessage({ on, active }: Flip): string {
  return active ? `gateway DNS ${on ? "on" : "off"} · applied to the live tunnel` : "saved — applies on next Connect";
}

/**
 * G1, DNS half: the gateway DNS switch (settings.dns_intercept), saved the moment it is flipped. The save re-applies the
 * tunnel inside the settings transaction, so it is a connection write (SETTINGS_CONNECTION_WRITE), optimistic with a
 * revert; a 502 rolled it back. It waits while any settings write or connection write runs.
 */
export function useGatewayDns() {
  const queryClient = useQueryClient();
  const settings = useQuery(queries.settings());   // read once; refetched after writes
  const putSettings = useApiWrite("putSettings");
  const settingsBusy = useSettingsBusy();
  const connectionBusy = useConnectionBusy();
  const busy = settingsBusy || connectionBusy;
  const mutation = useMutation({
    mutationKey: SETTINGS_CONNECTION_WRITE,
    mutationFn: ({ on }: Flip) => putSettings({ dns_intercept: on }),
    onMutate: ({ on }) => {
      const before = queryClient.getQueryData<Settings>(keys.settings)?.dns_intercept;
      queryClient.setQueryData<Settings>(keys.settings, (old) => (old ? { ...old, dns_intercept: on } : old));
      return { before };
    },
    onSuccess: (_saved, flip) => notifyOk(dnsSavedMessage(flip)),
    onError: (error, _flip, context) => {
      if (context?.before !== undefined) {
        const before = context.before;
        queryClient.setQueryData<Settings>(keys.settings, (old) => (old ? { ...old, dns_intercept: before } : old));
      }
      if (error instanceof ApiError && error.status === 502) {
        notifyError(null, saveRefusedMessage(error));
        void invalidateRefused(queryClient, keys.settings);
      } else if (isNoAnswer(error)) {
        notifyWarn(NO_ANSWER);
        void invalidateRefused(queryClient, keys.settings);
      } else {
        notifyError(error, "gateway DNS was not saved");
        void queryClient.invalidateQueries({ queryKey: keys.settings });
      }
    },
  });

  async function flip(on: boolean) {
    // Re-applying starts an xray the operator stopped while a node is still selected: ask first.
    if (!(await confirmTunnelStart(queryClient))) return;
    if (isConnectionBusy(queryClient) || isSettingsBusy(queryClient)) {
      notifyError(null, isConnectionBusy(queryClient) ? CONNECTION_BUSY : SETTINGS_BUSY);
      return;
    }
    const active = (queryClient.getQueryData<Status>(keys.status)?.active_node_id ?? null) !== null;
    mutation.mutate({ on, active });
  }

  return { settings, busy, flip };
}

/** The switch, named "Gateway DNS" by its label, never nested in another control. */
export function GatewayDnsSwitch({ dns }: { dns: ReturnType<typeof useGatewayDns> }) {
  return <Toggle label="Gateway DNS" checked={dns.settings.data?.dns_intercept ?? false} disabled={dns.busy || !dns.settings.data} onCheckedChange={(on) => void dns.flip(on)} />;
}

/** The note under the switch while client DNS is the gateway itself, which the intercept never sees. */
export function ClientDnsHint({ network, desktop }: { network: Network; desktop: boolean }) {
  const { client_dns: clientDns, ip } = network.segment;
  if (clientDns !== ip) return null;
  return (
    <p className="rounded-xl border border-line bg-glass px-3 py-2 text-xs leading-relaxed text-t2">
      {desktop
        ? <>Client DNS is the gateway itself (<span className="font-mono">{ip}</span>), a private destination — the intercept does not see client DNS.</>
        : "Resolve segment DNS in the gateway over DoH · the intercept does not see client DNS while it points at the gateway."}
    </p>
  );
}

/** The card's body on a desktop: the switch with its text, and the hint. */
export function GatewayDnsBody({ dns, network }: { dns: ReturnType<typeof useGatewayDns>; network: Network }) {
  const fallback = cardFallback([dns.settings], "Gateway DNS did not load", "h-12");
  return (
    fallback ?? (
      <div className="flex flex-col gap-2.5">
        <div className="flex items-start gap-3">
          <div className="pt-0.5"><GatewayDnsSwitch dns={dns} /></div>
          <div className="min-w-0">
            <p className="text-sm font-semibold text-t1">Resolve segment DNS in the gateway over DoH</p>
            <p className="text-xs text-t3">for nodes that don't relay UDP · saved as soon as you flip it</p>
          </div>
        </div>
        <ClientDnsHint network={network} desktop />
      </div>
    )
  );
}
