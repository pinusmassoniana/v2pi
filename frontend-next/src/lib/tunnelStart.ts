import type { QueryClient } from "@tanstack/react-query";
import type { Status } from "../api/client";
import { keys } from "../api/keys";
import { confirm } from "../components/confirm";

/** Asked before a write that re-applies the tunnel would start an xray the operator stopped. */
export const START_TUNNEL_CONFIRM = "This starts the tunnel again. Continue?";

/**
 * Whether a re-apply now would start a stopped xray: it was stopped on purpose (`xray_state` "stopped") while a node
 * is still selected, since only a selected node is re-applied. A status that is not known — never loaded, or its last
 * poll failed — counts as stopped, so the question is asked rather than skipped.
 */
export function startsStoppedTunnel(client: QueryClient): boolean {
  const state = client.getQueryState<Status>(keys.status);
  const status = state?.data;
  if (!status || state.status === "error") return true;
  return status.xray_state === "stopped" && status.active_node_id !== null;
}

/**
 * Ask START_TUNNEL_CONFIRM when the write would start a stopped xray; resolves false only when the operator declined.
 * The status is read from the cache at the moment of the click. Callers re-check their busy flags afterwards.
 */
export async function confirmTunnelStart(client: QueryClient): Promise<boolean> {
  if (!startsStoppedTunnel(client)) return true;
  return confirm(START_TUNNEL_CONFIRM, { confirmLabel: "Continue", danger: false });
}
