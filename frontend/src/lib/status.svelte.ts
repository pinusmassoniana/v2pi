// Shared gateway-status poller: ONE /api/status timer no matter how many views subscribe
// (the sidebar + the Dashboard previously polled it independently — OF2), and it pauses while
// the tab is hidden (OF1). Ref-counted; callers get a cleanup fn for their $effect.
import { api, type Status } from "./api";

let _status = $state<Status | null>(null);
let _stale = $state(false);   // last poll failed (transient) — value is last-good, not fresh
let _refs = 0;
let _timer: ReturnType<typeof setTimeout> | null = null;
let _inflight: Promise<void> | null = null;
let _controller: AbortController | null = null;
let _generation = 0;
let _scheduleGeneration = 0;
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
  if (_inflight) return _inflight;
  const generation = _generation;
  const controller = new AbortController();
  _controller = controller;
  _inflight = (async () => {
    try {
      const status = await api.getStatus(controller.signal);
      if (generation !== _generation) return;
      _status = status;
      _stale = false;
      // server_now===0 is a valid (epoch) value — test presence by type, not truthiness
      if (typeof status.server_now === "number") _skewMs = Date.now() - status.server_now * 1000;
    } catch {
      // transient blip (offline, timeout) must not blank the whole status UI — keep last-good, flag stale
      if (generation === _generation) _stale = true;
    } finally {
      if (generation === _generation) { _inflight = null; _controller = null; }
    }
  })();
  return _inflight;
}

/** Hard reset used on logout / lost session so the login screen never sees a prior session's status. */
export function resetStatus(): void {
  _generation++;
  _scheduleGeneration++;
  _controller?.abort();
  _controller = null;
  _inflight = null;
  if (_timer) clearTimeout(_timer);
  _timer = null;
  _refs = 0;
  _wanted.length = 0;
  document.removeEventListener("visibilitychange", onVisible);
  _status = null;
  _skewMs = 0;
  _stale = false;
}

function onVisible() {
  if (document.visibilityState === "visible") _retime(true);
}

// F2: every subscriber's requested cadence is tracked; the shared timer always runs at the
// fastest one (previously the first subscriber's interval silently won for everyone).
const _wanted: number[] = [];

function _retime(immediate = false) {
  const scheduleGeneration = ++_scheduleGeneration;
  if (_timer) { clearTimeout(_timer); _timer = null; }
  if (!_wanted.length) return;
  const ms = Math.min(..._wanted);
  const run = async () => {
    _timer = null;
    if (document.visibilityState === "visible") await pollStatusOnce();
    if (_wanted.length && scheduleGeneration === _scheduleGeneration)
      _timer = setTimeout(run, Math.min(..._wanted));
  };
  _timer = setTimeout(run, immediate ? 0 : ms);
}

export function subscribeStatus(intervalMs = 3000): () => void {
  _refs++;
  _wanted.push(intervalMs);
  if (_refs === 1) {
    document.addEventListener("visibilitychange", onVisible);
  }
  _retime(_refs === 1);
  return () => {
    const i = _wanted.indexOf(intervalMs);
    if (i !== -1) _wanted.splice(i, 1);
    if (--_refs <= 0) {
      _refs = 0;
      _wanted.length = 0;
      _scheduleGeneration++;
      if (_timer) clearTimeout(_timer);
      _timer = null;
      document.removeEventListener("visibilitychange", onVisible);
    } else {
      _retime();
    }
  };
}
