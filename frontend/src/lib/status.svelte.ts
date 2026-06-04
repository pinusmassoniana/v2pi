// Shared gateway-status poller: ONE /api/status timer no matter how many views subscribe
// (the sidebar + the Dashboard previously polled it independently — OF2), and it pauses while
// the tab is hidden (OF1). Ref-counted; callers get a cleanup fn for their $effect.
import { api, type Status } from "./api";

let _status = $state<Status | null>(null);
let _refs = 0;
let _timer: ReturnType<typeof setInterval> | null = null;

export const statusStore = {
  get value(): Status | null {
    return _status;
  },
};

export async function pollStatusOnce(): Promise<void> {
  try { _status = await api.getStatus(); } catch { _status = null; }
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
