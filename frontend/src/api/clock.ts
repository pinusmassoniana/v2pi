// Browser clock minus gateway clock, refreshed on every status read. Freshness and uptime labels
// render against the gateway's clock so a skewed phone clock cannot say "5 minutes ago" for "now".
let skewMs = 0;

export function recordServerNow(serverNowSec: number | undefined): void {
  // server_now === 0 is a valid epoch value — test presence by type, not truthiness
  if (typeof serverNowSec === "number") skewMs = Date.now() - serverNowSec * 1000;
}

export function serverNow(): number {
  return Date.now() - skewMs;
}

export function resetClock(): void {
  skewMs = 0;
}
