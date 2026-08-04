// Small shared status-line store — factors out the msg/msgKind/setMsg triplet that was pasted
// across five screens (Nodes/Subscriptions/Tuning/Routing/Settings). Not a notification
// framework: one message, one kind, nothing queued — screens with genuinely different shapes
// (RoadWarrior's ok()/fail(), Operations' three independent message slots) are left alone.
export type MsgKind = "ok" | "err";

export function createMsg(initial = "") {
  let text = $state(initial);
  let kind = $state<MsgKind>("ok");
  return {
    get text() { return text; },
    get kind() { return kind; },
    set(t: string, k: MsgKind = "ok") { text = t; kind = k; },
  };
}
