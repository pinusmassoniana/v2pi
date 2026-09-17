// B2: the words for the release check. Pure, and deliberately its own module — the command
// palette says them too, and importing them from PanelScreen would pull the whole System screen
// into the shell chunk.
import type { Diagnostics } from "../../api/client";
import { agoLabel } from "../../lib/dashboard";

/** The panel reports a newer release; it cannot install one (the container has no Docker socket). */
export const UPDATE_HINT = "Deploying is still yours: re-pin V2PI_IMAGE on the gateway and recreate the container.";

/** What the Check button says afterwards: what is available, or that everything is current. */
export function updateOutcome(data: Diagnostics): string {
  if (data.update_error) return `release check failed — ${data.update_error}`;
  const behind = [
    data.app_update_available ? `panel ${data.latest_app_version}` : "",
    data.xray_update_available ? `xray ${data.latest_xray_version}` : "",
  ].filter(Boolean);
  return behind.length ? `available: ${behind.join(" · ")}` : "already on the newest release";
}

/** "checked 3 h ago", "never checked", or the reason the last check did not land. */
export function updateCheckLine(
  data: Pick<Diagnostics, "update_checked_at" | "update_error" | "update_check_enabled">,
  nowSec: number,
): string {
  const when = data.update_checked_at === null
    ? "never checked"
    : `checked ${agoLabel(data.update_checked_at, nowSec)}`;
  const how = data.update_check_enabled ? "daily · through the tunnel when one is up" : "daily check off";
  return data.update_error ? `${when} · failed: ${data.update_error}` : `${when} · ${how}`;
}
