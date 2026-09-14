// How often each Home read is refreshed by its one polling owner (see src/api/live.ts).

/** Network: the status block, the connection path, the summaries and the event tail. */
export const NETWORK_POLL_MS = 4_000;
/** Nodes, node health, subscriptions and routing change rarely; a write refreshes them at once anyway. */
export const SLOW_POLL_MS = 30_000;
/** Recorded 24 h / 7 d history, polled only while such a window is selected. */
export const HISTORY_POLL_MS = 60_000;
/** Windows up to this long come from the live traffic store; longer ones from recorded history. */
export const LIVE_WINDOW_MAX_SEC = 3_600;
