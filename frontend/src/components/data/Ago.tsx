import { useEffect, useState } from "react";
import { serverNow } from "../../api/clock";
import { checkedAgo } from "../../lib/nodeHealth";

/** How often an {@link Ago} moves on: ages read in seconds and minutes, so a coarse tick is enough. */
export const AGO_TICK_MS = 15_000;

interface Ticker {
  timer: ReturnType<typeof setInterval>;
  listeners: Set<() => void>;
}

// One interval per period for the whole app, however many components read the time: started by its first
// subscriber, stopped with its last. A tick wakes every subscriber in the same batch.
const tickers = new Map<number, Ticker>();

function subscribe(ms: number, listener: () => void): () => void {
  let ticker = tickers.get(ms);
  if (!ticker) {
    const listeners = new Set<() => void>();
    ticker = { listeners, timer: setInterval(() => { for (const wake of listeners) wake(); }, ms) };
    tickers.set(ms, ticker);
  }
  const own = ticker;
  own.listeners.add(listener);
  return () => {
    own.listeners.delete(listener);
    if (own.listeners.size > 0) return;
    clearInterval(own.timer);
    if (tickers.get(ms) === own) tickers.delete(ms);
  };
}

/**
 * The gateway clock (serverNow, epoch ms), held in state and moved on by the shared ticker every `ms` — so a
 * component shows a current age without reading the clock while it renders, and re-renders on its own, not the
 * page around it.
 */
export function useNow(ms: number = AGO_TICK_MS): number {
  const [now, setNow] = useState(serverNow);
  useEffect(() => subscribe(ms, () => setNow(serverNow())), [ms]);
  return now;
}

/** "5 s ago" / "4 min ago" for an API timestamp, kept current by the shared ticker; `fallback` when there is none. */
export function Ago({ at, fallback = "—" }: { at: string | null | undefined; fallback?: string }) {
  const now = useNow();
  return <>{checkedAgo(at, now) ?? fallback}</>;
}
