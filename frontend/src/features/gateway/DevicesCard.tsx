import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Pencil, Pin, PinOff, X } from "lucide-react";
import { useState } from "react";
import type { Network, Reservation, Reservations } from "../../api/client";
import { useApiWrite, useConnectionBusy } from "../../api/invalidation";
import { queries } from "../../api/keys";
import { CardHeader } from "../../components/data/CardHeader";
import { cardFallback, type CardQuery } from "../../components/data/CardState";
import { Chip } from "../../components/data/Chip";
import { confirm } from "../../components/confirm";
import { Button } from "../../components/ui/Button";
import { GlassCard } from "../../components/ui/GlassCard";
import { Input } from "../../components/ui/Input";
import { notifyError, notifyOk } from "../../components/ui/Toaster";
import { cn } from "../../lib/cn";
import {
  MAX_NAME, PIN_HINT, UNPIN_CONFIRM, byUsage, deviceLabel, nameIssue, unpinnedLeases, usageText, windowText,
} from "./devices";

/** A4/B1: the pinned devices and the leases that could be pinned. Its own query, read once. */
export function useDevices() {
  const queryClient = useQueryClient();
  const reservations = useQuery(queries.reservations());
  const addReservation = useApiWrite("addReservation");
  const renameReservation = useApiWrite("renameReservation");
  const deleteReservation = useApiWrite("deleteReservation");
  const connectionBusy = useConnectionBusy();

  function keep(data: Reservations) {
    queryClient.setQueryData<Reservations>(queries.reservations().queryKey, data);
  }

  const pin = useMutation({
    mutationFn: ({ mac, ip, name }: { mac: string; ip: string; name: string }) => addReservation(mac, ip, name),
    onSuccess: (data, { ip }) => { keep(data); notifyOk(`${ip} pinned`); },
    onError: (error) => notifyError(error, "the device was not pinned"),
  });
  const rename = useMutation({
    mutationFn: ({ id, name }: { id: number; name: string }) => renameReservation(id, name),
    onSuccess: (data) => { keep(data); notifyOk("device renamed"); },
    onError: (error) => notifyError(error, "the device was not renamed"),
  });
  const unpin = useMutation({
    mutationFn: (id: number) => deleteReservation(id),
    onSuccess: (data) => { keep(data); notifyOk("device unpinned"); },
    onError: (error) => notifyError(error, "the device was not unpinned"),
  });

  return {
    query: reservations,
    data: reservations.data,
    busy: pin.isPending || rename.isPending || unpin.isPending || connectionBusy,
    pin: (mac: string, ip: string, name: string) => pin.mutate({ mac, ip, name }),
    rename: (id: number, name: string) => rename.mutate({ id, name }),
    unpin: async (id: number) => {
      if (await confirm(UNPIN_CONFIRM, { confirmLabel: "Unpin" })) unpin.mutate(id);
    },
  };
}

export type DevicesState = ReturnType<typeof useDevices>;

function PinnedRow({ device, devices, network }: { device: Reservation; devices: DevicesState; network: Network }) {
  const [editing, setEditing] = useState<string | null>(null);
  const problem = editing === null ? null : nameIssue(editing);
  // A device with no name of its own and no lease reads as its address, which the row already
  // shows — say nothing rather than print it twice.
  const named = deviceLabel(device, network.status.clients);
  const label = named === device.ip ? "—" : named;
  return (
    <li data-device-ip={device.ip} className="flex flex-wrap items-center gap-2 py-2 text-[13px] first:pt-0">
      <span className="w-32 shrink-0 font-mono font-semibold text-t1">{device.ip}</span>
      {editing === null ? (
        <span className="min-w-0 flex-1 truncate text-t2">{label}</span>
      ) : (
        <span className="flex min-w-0 flex-1 items-center gap-1.5">
          <Input
            aria-label={`Name of ${device.ip}`}
            value={editing}
            maxLength={MAX_NAME}
            aria-invalid={problem ? true : undefined}
            onChange={(event) => setEditing(event.target.value)}
            className={cn("h-8 text-xs", problem && "border-bad/60")}
          />
          <Button size="sm" variant="ghost" aria-label={`Save name of ${device.ip}`} disabled={Boolean(problem) || devices.busy}
            onClick={() => { devices.rename(device.id, editing.trim()); setEditing(null); }}>
            <Check size={13} aria-hidden />
          </Button>
          <Button size="sm" variant="ghost" aria-label={`Cancel renaming ${device.ip}`} onClick={() => setEditing(null)}>
            <X size={13} aria-hidden />
          </Button>
        </span>
      )}
      <span className="shrink-0 text-[11px] text-t3">{usageText(device)}</span>
      {device.online ? <Chip tone="ok">online</Chip> : <Chip plain tone="neutral">offline</Chip>}
      {editing === null ? (
        <span className="flex shrink-0 gap-1">
          <Button size="sm" variant="ghost" aria-label={`Rename ${device.ip}`} disabled={devices.busy}
            onClick={() => setEditing(device.name)}>
            <Pencil size={13} aria-hidden />
          </Button>
          <Button size="sm" variant="ghost" aria-label={`Unpin ${device.ip}`} disabled={devices.busy}
            onClick={() => void devices.unpin(device.id)}>
            <PinOff size={13} aria-hidden />
          </Button>
        </span>
      ) : null}
      {problem ? <p className="w-full text-[11px] text-bad">{problem}</p> : null}
    </li>
  );
}

export function DevicesCard({ network, devices, className }: {
  network: Network; devices: DevicesState; className?: string;
}) {
  const data = devices.data;
  const pinned = byUsage(data?.reservations ?? []);
  const candidates = unpinnedLeases(network.status.clients, data?.reservations ?? []);
  const full = pinned.length >= (data?.max_reservations ?? 64);
  return (
    <GlassCard aria-label="Devices" className={className}>
      <CardHeader
        title="Devices"
        detail={data ? `${pinned.length} pinned · traffic ${windowText(data.window_sec)}` : "pinned addresses"}
        aside={<Chip plain>DHCP + counters</Chip>}
      />
      {cardFallback([devices.query as CardQuery], "devices unavailable", "h-32") ?? (
        <>
          {pinned.length === 0 ? (
            <p className="text-sm text-t3">Nothing pinned yet — pin a lease below to name it, route it and count it.</p>
          ) : (
            <ul aria-label="Pinned devices" className="flex flex-col divide-y divide-line">
              {pinned.map((device) => (
                <PinnedRow key={device.id} device={device} devices={devices} network={network} />
              ))}
            </ul>
          )}
          {candidates.length ? (
            <div className="mt-3 border-t border-line pt-2.5">
              <p className="mb-1.5 text-[10.5px] font-semibold uppercase tracking-[.07em] text-t3">Not pinned</p>
              <ul aria-label="Unpinned leases" className="flex flex-col divide-y divide-line">
                {candidates.map((lease) => (
                  <li key={`${lease.mac}|${lease.ip}`} className="flex items-center gap-2 py-2 text-[13px] first:pt-0">
                    <span className="w-32 shrink-0 font-mono text-t2">{lease.ip}</span>
                    <span className="min-w-0 flex-1 truncate text-t3">{lease.hostname || "—"}</span>
                    <Button size="sm" disabled={devices.busy || full}
                      aria-label={`Pin ${lease.ip}`}
                      onClick={() => devices.pin(lease.mac, lease.ip, nameIssue(lease.hostname) ? "" : lease.hostname)}>
                      <Pin size={13} aria-hidden />Pin
                    </Button>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          <p className="mt-2 text-[11px] leading-relaxed text-t3">{full ? `At most ${data!.max_reservations} devices can be pinned. ` : ""}{PIN_HINT}</p>
        </>
      )}
    </GlassCard>
  );
}
