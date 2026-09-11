// Small shared status-line store — factors out the msg/msgKind/setMsg triplet that was pasted
// across five screens (Nodes/Subscriptions/Tuning/Routing/Settings). Not a notification
// framework: one message, one kind, nothing queued — screens with genuinely different shapes
// (RoadWarrior's ok()/fail(), Operations' three independent message slots) are left alone.
export type MsgKind = "ok" | "err";

// Messages used to sit on screen forever: nothing ever cleared them, so a transient "refresh
// failed" stayed under the header long after the next poll had succeeded, and read as the current
// state of the gateway. They expire instead — an error gets long enough to actually be read.
export const MSG_TTL_MS: Record<MsgKind, number> = { ok: 8000, err: 20000 };

export function createMsg(initial = "") {
  let text = $state(initial);
  let kind = $state<MsgKind>("ok");
  let timer: ReturnType<typeof setTimeout> | null = null;
  const clearTimer = () => { if (timer) { clearTimeout(timer); timer = null; } };
  return {
    get text() { return text; },
    get kind() { return kind; },
    set(t: string, k: MsgKind = "ok") {
      text = t; kind = k;
      clearTimer();
      if (t) timer = setTimeout(() => { text = ""; timer = null; }, MSG_TTL_MS[k]);
    },
    /** Drop the current message now — e.g. a retry succeeded and the old error is meaningless. */
    clear() { clearTimer(); text = ""; },
  };
}
