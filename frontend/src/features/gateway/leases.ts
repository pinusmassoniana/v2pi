// W7 DHCP leases: a stable order, a stable key, and the time each has left. Pure and unit-tested.
import type { DhcpClient } from "../../api/client";

/** A lease's React key: a device can move address, and one address can be handed to another device. */
export function leaseKey(lease: DhcpClient): string {
  return `${lease.mac}|${lease.ip}`;
}

function addressParts(ip: string): number[] {
  const v6 = ip.includes(":");
  return ip.split(v6 ? ":" : ".").map((part) => parseInt(part, v6 ? 16 : 10) || 0);
}

/** Leases in numeric address order (so .9 sorts before .10), so the list does not reshuffle on every poll. */
export function sortLeases(leases: readonly DhcpClient[]): DhcpClient[] {
  return [...leases].sort((a, b) => {
    const left = addressParts(a.ip);
    const right = addressParts(b.ip);
    for (let index = 0; index < Math.max(left.length, right.length); index++) {
      const difference = (left[index] ?? 0) - (right[index] ?? 0);
      if (difference) return difference;
    }
    return 0;
  });
}

/**
 * Time a lease has left, against the gateway clock (`nowMs`, epoch ms): floored to minutes under an hour, hours under a
 * day, days otherwise. `expiry` 0 is a lease that never expires (dnsmasq's infinite lease).
 */
export function leaseLeft(expiry: number, nowMs: number): string {
  if (expiry === 0) return "no expiry";
  const seconds = Math.max(0, Math.floor(expiry - nowMs / 1000));
  if (seconds < 3_600) return `${Math.floor(seconds / 60)}m left`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3_600)}h left`;
  return `${Math.floor(seconds / 86_400)}d left`;
}
