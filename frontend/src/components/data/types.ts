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

/** Live throughput of one outbound, bits per second each way. */
export interface PathRate { down: number; up: number }

/** The tunnel's (the proxy outbound's) and direct traffic's live rates. */
export interface OutboundRates { proxy: PathRate; direct: PathRate }

/** What the connection path says about the active node, and how its tunnel legs are drawn. */
export interface NodeSlot {
  /** none: no active node · stopped: xray is not running · no-frame: no live stats · bad / slow / ok: the real check · stale: anything else. */
  state: "none" | "stopped" | "no-frame" | "bad" | "slow" | "ok" | "stale";
  leg: PathLeg;
  /** The latency pill's words: "42 ms", "OK", "check failed", "xray stopped", "health stale", "—". */
  text: string;
  /** A smaller word after the text ("slow"); null for none. */
  note: string | null;
  tone: Tone;
  /** The fresh latency the pill shows while the check passes; null otherwise. */
  ms: number | null;
}
