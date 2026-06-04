// Shared gateway-status poller: ONE /api/status timer no matter how many views subscribe
// (the sidebar + the Dashboard previously polled it independently — OF2), and it pauses while
// the tab is hidden (OF1). Ref-counted; callers get a cleanup fn for their $effect.
import { api, type Status } from "./api";

let _status = $state<Status | null>(null);
let _refs = 0;
let _timer: ReturnType<typeof setInterval> | null = null;
// D4: Date.now() − (server wall-clock). Lets time labels (freshness/uptime) render against the
// Pi's clock instead of a possibly-skewed browser clock. Updated on every status poll.
let _skewMs = 0;

export const statusStore = {
  get value(): Status | null {
    return _status;
  },
};

/** Current time on the Pi's clock (ms), corrected for browser↔server skew (D4). */
export function serverNow(): number {
  return Date.now() - _skewMs;
}

export async function pollStatusOnce(): Promise<void> {
  try {
    _status = await api.getStatus();
    if (_status?.server_now) _skewMs = Date.now() - _status.server_now * 1000;
  } catch { _status = null; }
}

function onVisible() {
  if (document.visibilityState === "visible") pollStatusOnce();
}

export function subscribeStatus(intervalMs = 3000): () => void {
  _refs++;
  if (_refs === 1) {
    pollStatusOnce();
    _timer = setInterval(() => {
      if (document.visibilityState === "visible") pollStatusOnce();
    }, intervalMs);
    document.addEventListener("visibilitychange", onVisible);
  }
  return () => {
    if (--_refs <= 0) {
      _refs = 0;
      if (_timer) clearInterval(_timer);
      _timer = null;
      document.removeEventListener("visibilitychange", onVisible);
    }
  };
}
