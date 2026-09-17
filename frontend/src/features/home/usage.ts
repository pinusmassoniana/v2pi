// A7 Usage: the words and the arithmetic of "how much has this gateway moved". Pure and
// unit-tested; the gateway does the summing, this decides what it reads as.
import type { TrafficUsage } from "../../api/client";
import { compactBytes } from "../../lib/format";

export const USAGE_NOTE =
  "Tunnelled traffic only, counted on the gateway's own clock — a day ends at its midnight, not yours.";

export const CAP_NOTE =
  "The cap is a figure to watch, not a limit: nothing is throttled or disconnected when it is passed.";

export const NO_CAP = "no monthly cap set";

export type CapTone = "ok" | "warn" | "bad";

/** Amber from 80 % of the cap, red at 100 %. Without a cap there is nothing to colour. */
export function capTone(used: number, capBytes: number): CapTone | null {
  if (capBytes <= 0) return null;
  const share = used / capBytes;
  if (share >= 1) return "bad";
  return share >= 0.8 ? "warn" : "ok";
}

export function capFraction(used: number, capBytes: number): number | null {
  if (capBytes <= 0) return null;
  return Math.min(1, Math.max(0, used / capBytes));
}

/** "18.4 GB of 100 GB · resets on the 5th", or what there is when no cap is set. */
export function capText(usage: Pick<TrafficUsage, "month" | "cap_bytes" | "cap_reset_day">): string {
  if (usage.cap_bytes <= 0) return `${compactBytes(usage.month)} this month · ${NO_CAP}`;
  return `${compactBytes(usage.month)} of ${compactBytes(usage.cap_bytes)} · resets on the ${ordinal(usage.cap_reset_day)}`;
}

export function ordinal(day: number): string {
  const suffix = day % 10 === 1 && day !== 11 ? "st" : day % 10 === 2 && day !== 12 ? "nd" : day % 10 === 3 && day !== 13 ? "rd" : "th";
  return `${day}${suffix}`;
}

/** The four figures the card shows, in the order it shows them. */
export function usageRows(usage: TrafficUsage): { key: string; label: string; value: string }[] {
  return [
    { key: "today", label: "Today", value: compactBytes(usage.today) },
    { key: "week", label: "Last 7 days", value: compactBytes(usage.week) },
    { key: "month", label: "This month", value: compactBytes(usage.month) },
    { key: "last_month", label: "Last month", value: compactBytes(usage.last_month) },
  ];
}

/**
 * The daily bars, newest last, with the date each one is. `day` is a unix-day number on the
 * gateway's clock, so the label is built from the gateway's offset rather than the browser's —
 * otherwise a gateway in another timezone labels its own days wrong.
 */
export function usageDays(usage: TrafficUsage, limit = 30): { day: number; label: string; bytes: number }[] {
  return usage.days.slice(-limit).map((row) => ({
    day: row.day,
    // `day` already counts the GATEWAY's days (the query added its offset before dividing), so
    // the label is that day number read as a UTC date. Reading it in the browser's zone would
    // slide the whole row set by a day for anyone in another timezone.
    label: new Date(row.day * 86_400_000).toLocaleDateString([], { month: "short", day: "numeric", timeZone: "UTC" }),
    bytes: row.up_bytes + row.down_bytes,
  }));
}
