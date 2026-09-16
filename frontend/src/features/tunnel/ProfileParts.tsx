import type { TuningProfile } from "../../api/client";
import { cn } from "../../lib/cn";

export function ActiveBadge() {
  return <span className="whitespace-nowrap rounded-full bg-ok/12 px-2 py-0.5 text-[10.5px] font-bold text-ok">● active</span>;
}

export function DefaultBadge() {
  return <span className="whitespace-nowrap rounded-full border border-line bg-glass-2 px-2 py-0.5 text-[10.5px] font-semibold text-t2">default</span>;
}

/** A feature's on/off as a dot, with the word for screen readers (and as a tooltip). */
export function OnOff({ on, label }: { on: boolean; label: string }) {
  return (
    <span data-on={on} title={`${label} ${on ? "on" : "off"}`} className="inline-flex items-center gap-1.5">
      <span aria-hidden className={cn("size-2 rounded-full", on ? "bg-ok shadow-[0_0_8px_var(--ok)]" : "border border-t3")} />
      <span className="sr-only">{`${label} ${on ? "on" : "off"}`}</span>
    </span>
  );
}

/** "used by 3" or "—": explicit assignments only. */
export function usedBy(profile: Pick<TuningProfile, "node_count">): string {
  return profile.node_count > 0 ? String(profile.node_count) : "—";
}

/** The four features the list shows as dots, in column order. */
export const FEATURES = [
  ["frag", "frag_enabled"], ["noise", "noise_enabled"], ["mux", "mux_enabled"], ["DoH", "doh_enabled"],
] as const satisfies readonly (readonly [string, keyof TuningProfile])[];
