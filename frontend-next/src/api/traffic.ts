import { useSyncExternalStore } from "react";
import { api, type TrafficFrame, type TrafficHandle, type TrafficHistoryResp, type TrafficMessage } from "./client";

export interface TrafficSample { ts: number; up: number; down: number }

export interface TrafficSnapshot {
  /** Latest full frame, or null before the first one arrives. */
  live: TrafficFrame | null;
  /** Proxy-outbound samples, oldest first, at most RING_SIZE. Mutated in place — memoise on `version`. */
  samples: TrafficSample[];
  /** The server reported traffic stats disabled. */
  disabled: boolean;
  /** Bumps on every change; the snapshot object is replaced with it. */
  version: number;
}

export const RING_SIZE = 4000;
const HISTORY_WINDOW_SEC = 3600;
const HISTORY_MAX_POINTS = 1200;

type Connect = (onMessage: (m: TrafficMessage) => void, onGap?: () => void) => TrafficHandle;
type LoadHistory = () => Promise<TrafficHistoryResp>;

export function createTrafficStore(
  connect: Connect = (onMessage, onGap) => api.connectTraffic(onMessage, onGap),
  loadHistory: LoadHistory = () => api.getTrafficHistory(HISTORY_WINDOW_SEC, HISTORY_MAX_POINTS),
) {
  // One array for the life of the store: copying 4000 samples every second is what the Svelte
  // dashboard learned to avoid. Subscribers re-render off `version` instead.
  const samples: TrafficSample[] = [];
  let snapshot: TrafficSnapshot = { live: null, samples, disabled: false, version: 0 };
  const listeners = new Set<() => void>();
  let handle: TrafficHandle | null = null;
  // Bumped by reset(), so a history request from the ended session cannot refill the window.
  let epoch = 0;

  const emit = (patch: Partial<Pick<TrafficSnapshot, "live" | "disabled">>) => {
    snapshot = { ...snapshot, ...patch, samples, version: snapshot.version + 1 };
    for (const listener of listeners) listener();
  };

  const trim = () => {
    if (samples.length > RING_SIZE) samples.splice(0, samples.length - RING_SIZE);
  };

  // Fill the window from recorded history after the first connect and after every gap.
  const backfill = async () => {
    const started = epoch;
    try {
      const history = await loadHistory();
      if (started !== epoch) return;
      const byTs = new Map<number, TrafficSample>();
      for (const [ts, up, down] of history.samples) byTs.set(ts, { ts, up, down });
      for (const sample of samples) byTs.set(sample.ts, sample);   // live samples win a tie
      const merged = [...byTs.values()].sort((a, b) => a.ts - b.ts);
      samples.length = 0;
      for (const sample of merged) samples.push(sample);
      trim();
      emit({});
    } catch {
      // history is best-effort; the live stream keeps flowing
    }
  };

  const onMessage = (message: TrafficMessage) => {
    if ("disabled" in message) { emit({ disabled: true }); return; }
    if ("error" in message) return;   // a transient stats error just leaves a gap
    const proxy = message.outbounds.proxy ?? { up_bps: 0, down_bps: 0 };
    samples.push({ ts: message.ts, up: proxy.up_bps, down: proxy.down_bps });
    trim();
    emit({ live: message, disabled: false });
  };

  return {
    subscribe(listener: () => void): () => void {
      listeners.add(listener);
      if (!handle) {
        handle = connect(onMessage, () => { void backfill(); });
        void backfill();
      }
      return () => {
        listeners.delete(listener);
        if (listeners.size === 0 && handle) {
          handle.close();
          handle = null;
        }
      };
    },
    /** End of session: close the socket now and forget the frame, the samples and the disabled flag. */
    reset(): void {
      epoch += 1;
      handle?.close();
      handle = null;
      samples.length = 0;
      emit({ live: null, disabled: false });
    },
    getSnapshot(): TrafficSnapshot {
      return snapshot;
    },
  };
}

export const trafficStore = createTrafficStore();

export function useTraffic(): TrafficSnapshot {
  return useSyncExternalStore(trafficStore.subscribe, trafficStore.getSnapshot);
}
