// B3: the words and numbers of one on-demand diagnosis. Pure and unit-tested; the card renders
// these, so what the panel claims about a measurement is decided in one place.
import type { Diagnosis } from "../../api/client";

export const DIAGNOSE_HINT =
  "Measures this node's connection in phases — TCP, the TLS handshake, the first byte back, then a bounded "
  + "transfer. Nothing is scheduled: it runs when you press it, and it keeps no result.";

/** The one claim the card must never make is "you are being throttled". */
export const DIAGNOSE_NOTE =
  "A heuristic, not proof. A stalled transfer is what throttling looks like from here — and also what a busy "
  + "server or a lossy uplink looks like. Compare two nodes before concluding anything about one.";

export const RUNNING_ELSEWHERE = "a diagnosis is already running";

export const VERDICT_TONE: Record<Diagnosis["verdict"], "ok" | "warn" | "bad"> = {
  ok: "ok", slow: "warn", stalls: "warn", down: "bad",
};

export const VERDICT_LABEL: Record<Diagnosis["verdict"], string> = {
  ok: "works", slow: "slow", stalls: "stalls mid-stream", down: "no traffic",
};

/** "312 ms", or "—" for a phase that never ran (there is no timing to invent for it). */
export function msText(value: number | null): string {
  return value === null ? "—" : `${value} ms`;
}

/** "4.2 Mbit/s" / "180 kbit/s" — the unit people read a link in, not bytes. */
export function rateText(kbps: number | null): string {
  if (kbps === null || kbps <= 0) return "—";
  return kbps >= 1000 ? `${(kbps / 1000).toFixed(1)} Mbit/s` : `${kbps} kbit/s`;
}

/** "256 KB of 256 KB" — the shortfall IS the finding, so both numbers are shown. */
export function transferText(result: Pick<Diagnosis, "bytes" | "requested_bytes">): string {
  const kb = (bytes: number) => `${Math.round(bytes / 1024)} KB`;
  return `${kb(result.bytes)} of ${kb(result.requested_bytes)}`;
}

export interface PhaseRow { key: string; value: string; sub?: string }

/** The four phases in the order they happen, so a row that reads "—" says where it stopped. */
export function phaseRows(result: Diagnosis): PhaseRow[] {
  return [
    { key: "TCP", value: msText(result.tcp_ms), sub: "connect to the node" },
    { key: "HANDSHAKE", value: msText(result.tls_ms), sub: result.tls_ms === null && result.tcp_ms !== null ? "no TLS layer" : "TLS with this node's SNI" },
    { key: "FIRST BYTE", value: msText(result.ttfb_ms), sub: "through the tunnel" },
    { key: "TRANSFER", value: rateText(result.kbps), sub: transferText(result) },
  ];
}
