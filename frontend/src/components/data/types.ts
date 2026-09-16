import type { PillTone } from "../ui/Pill";

/** The state colour of a value. Colour means state; the brand gradient is never a tone. */
export type Tone = PillTone;

/** One row of LatencyBars: a node, what its last probe said, and how old that is. */
export interface LatencyRowData {
  id: number;
  name: string;
  /** Flag of the egress country a probe reported; "" when no probe said. */
  flag: string;
  /** The latency to draw; null when there is no number to show. */
  ms: number | null;
  /**
   * live: the active node's fresh real check · measured: a background-sweep result · failed: the last
   * real check failed · stale: the active node's health is stale · not-probed: no result at all.
   */
  state: "live" | "measured" | "failed" | "stale" | "not-probed";
  /** Time since the probe, e.g. "4 min"; null when it does not apply. */
  age: string | null;
  /** The probe is older than ten minutes. */
  dim: boolean;
  active: boolean;
}

export type EventLevel = "ok" | "warn" | "bad" | "info";

/** The connection path's tunnel leg: carrying healthy traffic, failing, or not in use. */
export type PathLeg = "ok" | "bad" | "off";
