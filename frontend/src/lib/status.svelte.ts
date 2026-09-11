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
// When the last poll actually succeeded. The offline banner shows it so "the panel is unreachable"
// comes with "…and what you are looking at is from 21:14" instead of a bare warning.
let _lastOkAt = $state<number | null>(null);

export const statusStore = {
  get value(): Status | null {
    return _status;
  },
  /** true when the last poll failed — the value is the last-good snapshot, not current. */
  get stale(): boolean {
    return _stale;
  },
  /** Browser-clock ms of the last successful poll, or null if none has landed yet. */
  get lastOkAt(): number | null {
    return _lastOkAt;
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
      _lastOkAt = Date.now();
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
  _dueAt = 0;
  _refs = 0;
  _wanted.length = 0;
  document.removeEventListener("visibilitychange", onVisible);
  _status = null;
  _skewMs = 0;
  _stale = false;
  _lastOkAt = null;
}

function onVisible() {
  if (document.visibilityState === "visible") _retime(true);
}

// F2: every subscriber's requested cadence is tracked; the shared timer always runs at the
// fastest one (previously the first subscriber's interval silently won for everyone).
const _wanted: number[] = [];

// When the pending poll is due (browser clock). Rescheduling must never push it further out.
let _dueAt = 0;

function _retime(immediate = false) {
  if (!_wanted.length) {
    _scheduleGeneration++;
    if (_timer) { clearTimeout(_timer); _timer = null; }
    return;
  }
  const ms = Math.min(..._wanted);
  const wantAt = immediate ? 0 : Date.now() + ms;
  // A second subscriber used to cancel the first one's *immediate* poll and reschedule it a full
  // interval out: mounting the Dashboard (which subscribes after the shell does) left the whole
  // panel without a status for 3 s at every start, and the offline banner just as long to appear.
  // Only ever reschedule to something sooner.
  if (_timer && _dueAt <= wantAt) return;
  const scheduleGeneration = ++_scheduleGeneration;
  if (_timer) { clearTimeout(_timer); _timer = null; }
  const run = async () => {
    _timer = null;
    if (document.visibilityState === "visible") await pollStatusOnce();
    if (_wanted.length && scheduleGeneration === _scheduleGeneration) {
      const next = Math.min(..._wanted);
      _dueAt = Date.now() + next;
      _timer = setTimeout(run, next);
    }
  };
  _dueAt = wantAt;
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
      _dueAt = 0;
      document.removeEventListener("visibilitychange", onVisible);
    } else {
      _retime();
    }
  };
}
