// Keeps a screen's data live so nothing needs a page reload to be seen.
//
// One helper, three triggers:
//   • an interval, while the tab is visible — the server changes things on its own (a subscription
//     auto-update replaces the node list, health failover switches the active node, a lease expires);
//   • the tab coming back to the front — no waiting out a whole interval after a long background;
//   • any successful write from anywhere in the app (DATA_CHANGED_EVENT, dispatched by api.mutate)
//     — a sibling screen's action lands here immediately instead of on the next tick. Nodes and
//     Subscriptions share the "nodes" screen, so refreshing a subscription must repaint the table
//     next to it.
//
// `paused()` lets a screen with an edited form opt out: reloading under the operator would throw
// away what they typed. Screens that only display (Nodes, Dashboard, Health) never pause.
import { untrack } from "svelte";
import { DATA_CHANGED_EVENT } from "./api";

export function subscribeLive(
  load: () => void | Promise<void>,
  ms: number,
  paused?: () => boolean,
): () => void {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let stopped = false;
  let running = false;
  let loadedOnce = false;

  const run = async () => {
    timer = null;
    if (running || stopped) return;
    running = true;
    try {
      // paused() protects edits in progress — there are none before the first load, and a screen
      // whose dirty flag reads "true" against an empty baseline would otherwise never load at all.
      if (document.visibilityState === "visible" && (!loadedOnce || !paused?.())) {
        loadedOnce = true;
        await load();
      }
    } finally {
      running = false;
      if (!stopped) timer = setTimeout(run, ms);
    }
  };
  // out-of-band trigger: reload now and restart the interval from now
  const now = () => {
    if (stopped || running || document.visibilityState !== "visible") return;
    if (loadedOnce && paused?.()) return;
    if (timer) clearTimeout(timer);
    void run();
  };

  // untrack: callers mount this straight inside an $effect, and both load() and paused() read
  // component state. Letting that first synchronous run register dependencies would make the
  // effect re-subscribe every time the data it just loaded changes — an endless reload that also
  // wipes anything staged into the screen's form.
  untrack(() => { void run(); });
  document.addEventListener("visibilitychange", now);
  document.addEventListener(DATA_CHANGED_EVENT, now);
  return () => {
    stopped = true;
    if (timer) clearTimeout(timer);
    document.removeEventListener("visibilitychange", now);
    document.removeEventListener(DATA_CHANGED_EVENT, now);
  };
}
