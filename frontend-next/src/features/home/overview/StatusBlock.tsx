import { useMutation } from "@tanstack/react-query";
import type { Network, Node, Status } from "../../../api/client";
import { useApiWrite } from "../../../api/invalidation";
import { useTraffic } from "../../../api/traffic";
import { openPalette } from "../../../app/shell/palette";
import { confirm } from "../../../components/confirm";
import { Chip } from "../../../components/data/Chip";
import { StatusOrb } from "../../../components/data/StatusOrb";
import { Uptime } from "../../../components/data/Uptime";
import { Button } from "../../../components/ui/Button";
import { GlassCard } from "../../../components/ui/GlassCard";
import { Pill } from "../../../components/ui/Pill";
import { Skeleton } from "../../../components/ui/States";
import { notifyError, notifyOk } from "../../../components/ui/Toaster";
import { cn } from "../../../lib/cn";
import {
  activeFlag, activeNode, hasConfigDrift, killSwitchState, liveLatency, nodeEndpoint, poolSize, tunnelLabel, xrayLabel,
} from "../derive";

export interface StatusBlockProps {
  status: Status | undefined;
  /** The status poll is failing: values are the last known ones, dimmed, and the tunnel reads UNKNOWN. */
  statusError: boolean;
  network: Network | undefined;
  nodes: Node[] | undefined;
  className?: string;
}

/** O4 + O12: the tunnel, the node, the chips, and Switch node / Roll back / Disconnect. */
export function StatusBlock({ status, statusError, network, nodes, className }: StatusBlockProps) {
  const traffic = useTraffic();
  const rollbackWrite = useApiWrite("rollback");
  const disconnectWrite = useApiWrite("disconnect");
  const rollback = useMutation({
    mutationFn: (target: string) => rollbackWrite().then((result) => ({ ok: Boolean(result?.ok), target })),
    onSuccess: ({ ok, target }) => notifyOk(ok ? `Rolled back to ${target}` : "Nothing to roll back"),
    onError: (error) => notifyError(error, "roll back failed"),
  });
  const disconnect = useMutation({
    mutationFn: ({ id }: { id: number; name: string }) => disconnectWrite(id),
    onSuccess: (_result, { name }) => notifyOk(`Disconnected from ${name}`),
    onError: (error) => notifyError(error, "disconnect failed"),
  });

  if (!status) {
    return (
      <GlassCard aria-label="Status" aria-busy className={className}>
        <Skeleton className="h-36" />
      </GlassCard>
    );
  }

  const frame = traffic.disabled ? null : traffic.live;
  const tunnel = tunnelLabel(status, statusError);
  const online = tunnel.label === "ONLINE";
  const latency = liveLatency(frame?.active ?? null);
  const active = activeNode(nodes, status.active_node_id);
  const activeName = active?.name ?? (status.active_node_id === null ? null : `node ${status.active_node_id}`);
  const flag = activeFlag(status.active_node_id, frame?.active ?? null);
  const prevId = status.prev_active_node_id;
  const prevName = prevId !== null && prevId !== status.active_node_id
    ? (activeNode(nodes, prevId)?.name ?? `node ${prevId}`)
    : null;
  const xray = xrayLabel(status);
  const kill = killSwitchState(network);
  const pool = poolSize(network?.segment);
  const clients = network?.status.dhcp_clients;
  const busy = rollback.isPending || disconnect.isPending;

  const orb = online
    ? { value: latency === null ? "—" : String(latency), caption: latency === null ? "ONLINE" : "ms · ONLINE", tone: "ok" as const }
    : tunnel.label === "UNKNOWN"
      ? { value: "—", caption: "UNKNOWN", tone: "neutral" as const }
      : { value: "×", caption: "OFFLINE", tone: "bad" as const };

  async function onRollback(target: string) {
    if (await confirm(`Roll back the live config to ${target}?`, { confirmLabel: "Roll back" })) rollback.mutate(target);
  }

  async function onDisconnect(id: number, name: string) {
    if (await confirm(`Disconnect from ${name}? Devices lose the tunnel until a node is connected again.`, { confirmLabel: "Disconnect" })) {
      disconnect.mutate({ id, name });
    }
  }

  return (
    <GlassCard aria-label="Status" data-stale={statusError || undefined} className={cn("transition-opacity duration-200", statusError && "opacity-60", className)}>
      <div className="flex items-center gap-4">
        <StatusOrb value={orb.value} caption={orb.caption} tone={orb.tone} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2.5">
            <Pill tone={tunnel.tone} dot>Tunnel {tunnel.label}</Pill>
            <span className="text-[11.5px] text-t3">uptime <Uptime since={status.active_since} running={online} className="text-t1" /></span>
          </div>
          <p className="mt-1.5 truncate text-[19px] font-bold tracking-tight md:text-[21px]">
            {flag ? `${flag} ` : ""}{activeName ?? "No node connected"}
          </p>
          {active ? <p className="truncate text-xs text-t2">{nodeEndpoint(active)}</p> : null}
          <div className="mt-2.5 flex flex-wrap gap-1.5">
            <Chip tone={xray.tone}>xray <b>{xray.label}</b></Chip>
            {hasConfigDrift(status) ? <Chip tone="bad" plain>stale config</Chip> : null}
            <Chip tone={kill.tone}>kill-switch <b>{kill.label}</b></Chip>
            <Chip plain>clients <b>{clients ?? "—"}{clients !== undefined && pool !== null ? ` / ${pool}` : ""}</b></Chip>
          </div>
        </div>
      </div>
      <div className="mt-3.5 grid grid-cols-2 gap-2 border-t border-line pt-3 md:flex">
        <Button variant="primary" className="col-span-2" onClick={() => openPalette("nodes")}>
          Switch node <kbd aria-hidden className="hidden rounded-md bg-bg/20 px-1.5 text-[10.5px] md:inline">⌘K</kbd>
        </Button>
        {prevName !== null ? (
          <Button disabled={busy} onClick={() => void onRollback(prevName)}>
            <span aria-hidden>↺</span> {rollback.isPending ? "Rolling back…" : `Roll back to ${prevName}`}
          </Button>
        ) : null}
        {status.active_node_id !== null ? (
          <Button variant="danger" className="md:ml-auto" disabled={busy} onClick={() => void onDisconnect(status.active_node_id!, activeName!)}>
            {disconnect.isPending ? "Disconnecting…" : "Disconnect"}
          </Button>
        ) : null}
      </div>
    </GlassCard>
  );
}
