import { fmtUptimeCoarse } from "../../lib/format";
import { useNow } from "./Ago";

/**
 * "3d 4h" since `since` (epoch seconds) on the gateway clock, moved on by the shared ticker — it never reads the clock
 * while rendering, adds no interval of its own, and re-renders only itself. "—" without a start.
 */
export function ConnectedFor({ since }: { since: number | null }) {
  const now = useNow();
  return <>{since === null ? "—" : fmtUptimeCoarse(Math.floor(now / 1000) - since)}</>;
}
