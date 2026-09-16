import { useCallback, useMemo } from "react";
import type { TrafficFrame } from "../../api/client";
import { queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { useTraffic, type TrafficSample } from "../../api/traffic";
import type { TrafficWindowSec } from "../../components/data/TrafficChart";
import { windowSlice } from "../../components/data/trafficGeometry";
import { HISTORY_POLL_MS, LIVE_WINDOW_MAX_SEC } from "../../api/cadence";

export interface TrafficSeries {
  /** The selected window, oldest first. */
  samples: TrafficSample[];
  /** A long window's history has not arrived yet. */
  pending: boolean;
  /** A long window's history failed to load. */
  error: Error | null;
  retry: () => void;
  /** The gateway reports traffic stats switched off. */
  disabled: boolean;
  /** The latest live frame, null while none has arrived or stats are off. */
  live: TrafficFrame | null;
}

/**
 * One throughput series for the chart. Windows up to an hour read the live store (seeded with an hour of
 * history, then fed by the WebSocket); 24 h and 7 d read recorded history, polled every minute only while
 * that window is selected — switching away leaves the query without an enabled observer, so it stops.
 */
export function useTrafficSeries(windowSec: TrafficWindowSec): TrafficSeries {
  const traffic = useTraffic();
  const long = windowSec > LIVE_WINDOW_MAX_SEC;
  const history = usePolledQuery({ ...queries.trafficHistory(windowSec), enabled: long }, HISTORY_POLL_MS);
  const recorded = useMemo(
    () => (history.data?.samples ?? []).map(([ts, up, down]) => ({ ts, up, down })),
    [history.data],
  );
  // Two windows, memoised separately so a live frame — which bumps `traffic.version` roughly once a
  // second — only ever invalidates the live-window slice. `traffic.samples` is one ring array mutated
  // in place for the store's life (createTrafficStore in api/traffic.ts), so its own reference never
  // changes; `traffic.version` is the store's designated change signal, so it stands in for it here.
  // The recorded-history slice depends on nothing a live frame touches, so it — and the chart geometry
  // built from it — stays referentially stable between history polls.
  const { version: liveVersion } = traffic;
  const liveSamples = useMemo(() => {
    void liveVersion; // read only to key the memo — the ring array itself never changes reference
    return windowSlice(traffic.samples, windowSec);
  }, [traffic.samples, liveVersion, windowSec]);
  const recordedSamples = useMemo(() => windowSlice(recorded, windowSec), [recorded, windowSec]);
  const { refetch } = history;
  const retry = useCallback(() => void refetch(), [refetch]);
  return {
    samples: long ? recordedSamples : liveSamples,
    pending: long && history.isPending,
    error: long && history.isError ? history.error : null,
    retry,
    disabled: traffic.disabled,
    live: traffic.disabled ? null : traffic.live,
  };
}
