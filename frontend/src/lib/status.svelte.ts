// Shared gateway-status poller: ONE /api/status timer no matter how many views subscribe
// (the sidebar + the Dashboard previously polled it independently — OF2), and it pauses while
// the tab is hidden (OF1). Ref-counted; callers get a cleanup fn for their $effect.
import { api, type Status } from "./api";

let _status = $state<Status | null>(null);
let _stale = $state(false);   // last poll failed (transient) — value is last-good, not fresh
let _refs = 0;
let _timer: ReturnType<typeof setInterval> | null = null;
// D4: Date.now() − (server wall-clock). Lets time labels (freshness/uptime) render against the
// Pi's clock instead of a possibly-skewed browser clock. Updated on every status poll.
let _skewMs = 0;

export const statusStore = {
  get value(): Status | null {
    return _status;
  },
  /** true when the last poll failed — the value is the last-good snapshot, not current. */
  get stale(): boolean {
    return _stale;
  },
};

/** Current time on the Pi's clock (ms), corrected for browser↔server skew (D4). */
export function serverNow(): number {
  return Date.now() - _skewMs;
}

export async function pollStatusOnce(): Promise<void> {
  try {
    _status = await api.getStatus();
    _stale = false;
    // server_now===0 is a valid (epoch) value — test presence by type, not truthiness
    if (_status && typeof _status.server_now === "number") _skewMs = Date.now() - _status.server_now * 1000;
  } catch {
    // transient blip (offline, timeout) must not blank the whole status UI — keep last-good, flag stale
    _stale = true;
  }
}

/** Hard reset used on logout / lost session so the login screen never sees a prior session's status. */
export function resetStatus(): void {
  if (_timer) clearInterval(_timer);
  _timer = null;
  _refs = 0;
  _wanted.length = 0;
  document.removeEventListener("visibilitychange", onVisible);
  _status = null;
  _skewMs = 0;
  _stale = false;
}

function onVisible() {
  if (document.visibilityState === "visible") pollStatusOnce();
}

// F2: every subscriber's requested cadence is tracked; the shared timer always runs at the
// fastest one (previously the first subscriber's interval silently won for everyone).
const _wanted: number[] = [];

function _retime() {
  if (_timer) { clearInterval(_timer); _timer = null; }
  if (!_wanted.length) return;
  const ms = Math.min(..._wanted);
  _timer = setInterval(() => {
    if (document.visibilityState === "visible") pollStatusOnce();
  }, ms);
}

export function subscribeStatus(intervalMs = 3000): () => void {
  _refs++;
  _wanted.push(intervalMs);
  if (_refs === 1) {
    pollStatusOnce();
    document.addEventListener("visibilitychange", onVisible);
  }
  _retime();
  return () => {
    const i = _wanted.indexOf(intervalMs);
    if (i !== -1) _wanted.splice(i, 1);
    if (--_refs <= 0) {
      _refs = 0;
      _wanted.length = 0;
      if (_timer) clearInterval(_timer);
      _timer = null;
      document.removeEventListener("visibilitychange", onVisible);
    } else {
      _retime();
    }
  };
}
