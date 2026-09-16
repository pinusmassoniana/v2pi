// How often each read is refreshed by its one polling owner (see src/api/live.ts), for every screen.

/** Network: Home's status block, connection path, summaries and event tail. */
export const NETWORK_POLL_MS = 4_000;
/** Network on Gateway › Network: the segment form, leases and host enforcement. A GET probes the uplink, so not faster. */
export const GATEWAY_NETWORK_POLL_MS = 5_000;
/** Remote access: the inbound, its clients and whether it is live. */
export const RW_POLL_MS = 15_000;
/** Nodes, node health, subscriptions and routing change rarely; a write refreshes them at once anyway. */
export const SLOW_POLL_MS = 30_000;
/** The log pane, only while its auto-refresh is on: the Svelte panel's own interval. */
export const LOGS_POLL_MS = 5_000;
/** Recorded 24 h / 7 d history, polled only while such a window is selected. */
export const HISTORY_POLL_MS = 60_000;
/** Windows up to this long come from the live traffic store; longer ones from recorded history. */
export const LIVE_WINDOW_MAX_SEC = 3_600;
