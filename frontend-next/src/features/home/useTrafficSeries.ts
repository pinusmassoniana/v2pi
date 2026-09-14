import { useMemo } from "react";
import type { TrafficFrame } from "../../api/client";
import { queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { useTraffic, type TrafficSample } from "../../api/traffic";
import type { TrafficWindowSec } from "../../components/data/TrafficChart";
import { windowSlice } from "../../components/data/trafficGeometry";
import { HISTORY_POLL_MS, LIVE_WINDOW_MAX_SEC } from "./cadence";

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
  return {
    samples: windowSlice(long ? recorded : traffic.samples, windowSec),
    pending: long && history.isPending,
    error: long && history.isError ? history.error : null,
    retry: () => void history.refetch(),
    disabled: traffic.disabled,
    live: traffic.disabled ? null : traffic.live,
  };
}
