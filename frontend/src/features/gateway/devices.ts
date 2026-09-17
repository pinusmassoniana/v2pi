// A4/B1 Devices: which leases are still unpinned, what a pinned device's name and traffic read as,
// and the words the card uses. Pure and unit-tested; no rendering here.
import type { DhcpClient, Reservation } from "../../api/client";
import { compactBytes } from "../../lib/format";
import { sortLeases } from "./leases";

/** backend ReservationIn.name / MAX_RESERVATIONS */
export const MAX_NAME = 32;

export const PIN_HINT =
  "A pinned device always gets the same address from this gateway, which is what lets a routing rule "
  + "and a traffic counter name it. Pinning restarts the segment's DHCP server; devices keep their "
  + "current address until they renew.";

export const UNPIN_CONFIRM =
  "Unpin this device? It goes back to any free address, and its traffic stops being counted. "
  + "A routing rule naming that address is left as it is.";

export const NAME_MESSAGE = "letters, digits and hyphens only";

/** The backend's `_HOSTNAME_RE`, so a name is refused here before it reaches dnsmasq's config. */
const NAME_RE = /^[A-Za-z0-9]([A-Za-z0-9-]{0,30}[A-Za-z0-9])?$/;

export function nameIssue(name: string): string | null {
  const trimmed = name.trim();
  if (!trimmed) return null;                       // a nameless reservation is fine
  return NAME_RE.test(trimmed) ? null : NAME_MESSAGE;
}

/** Leases with no reservation yet, in address order — the ones the card offers a Pin button for. */
export function unpinnedLeases(leases: readonly DhcpClient[], reservations: readonly Reservation[]): DhcpClient[] {
  const pinned = new Set(reservations.map((r) => r.mac.toLowerCase()));
  return sortLeases(leases.filter((lease) => !pinned.has(lease.mac.toLowerCase())));
}

/** What a pinned device is called: its own name, else the hostname its lease carries, else its address. */
export function deviceLabel(reservation: Reservation, leases: readonly DhcpClient[] = []): string {
  if (reservation.name) return reservation.name;
  const lease = leases.find((item) => item.mac.toLowerCase() === reservation.mac.toLowerCase());
  return lease?.hostname || reservation.ip;
}

/** "1.2 GB down · 240 MB up", or the honest "nothing yet" for a device that has not been seen. */
export function usageText(reservation: Pick<Reservation, "up_bytes" | "down_bytes">): string {
  const total = reservation.up_bytes + reservation.down_bytes;
  if (total <= 0) return "no traffic recorded";
  return `${compactBytes(reservation.down_bytes)} down · ${compactBytes(reservation.up_bytes)} up`;
}

/** "in the last 24 h" / "in the last 60 min", for whatever window the gateway reported. */
export function windowText(windowSec: number): string {
  if (windowSec % 86_400 === 0) return `in the last ${windowSec / 86_400} d`;
  if (windowSec % 3_600 === 0) return `in the last ${windowSec / 3_600} h`;
  return `in the last ${Math.max(1, Math.round(windowSec / 60))} min`;
}

/** Busiest first, so the card answers "who is using the bandwidth" without reading every row. */
export function byUsage(reservations: readonly Reservation[]): Reservation[] {
  return [...reservations].sort(
    (a, b) => (b.up_bytes + b.down_bytes) - (a.up_bytes + a.down_bytes) || a.ip.localeCompare(b.ip));
}
