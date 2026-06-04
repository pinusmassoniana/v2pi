// Pure, unit-tested helpers for the Dashboard (kept out of the .svelte file so they're testable).
import type { Subscription } from "./api";

export interface SubWarn { name: string; text: string; level: "warn" | "bad"; }

/** Subscription expiry / data-cap warnings (audit feature E). `nowSec` = current epoch seconds.
 *  Expiry: `expire_at` within 3 days (warn) or past (bad). Cap: used/total ≥ 80% (warn) / ≥ 100% (bad),
 *  where used = up+down and total = the provider's Subscription-Userinfo allowance. */
export function subWarnings(subs: Subscription[], nowSec: number): SubWarn[] {
  const out: SubWarn[] = [];
  for (const s of subs) {
    if (!s.enabled) continue;
    if (s.expire_at && s.expire_at > 0) {
      const days = (s.expire_at - nowSec) / 86400;
      if (days <= 0) out.push({ name: s.name, text: "expired", level: "bad" });
      else if (days <= 3) out.push({ name: s.name, text: `expires in ${Math.ceil(days)}d`, level: "warn" });
    }
    if (s.total_bytes && s.total_bytes > 0) {
      const used = (s.up_bytes ?? 0) + (s.down_bytes ?? 0);
      const pct = used / s.total_bytes;
      if (pct >= 1) out.push({ name: s.name, text: "data cap reached", level: "bad" });
      else if (pct >= 0.8) out.push({ name: s.name, text: `${Math.round(pct * 100)}% of data cap`, level: "warn" });
    }
  }
  return out;
}

/** SVG path for a tiny latency sparkline (audit feature B); "" when there's too little data. */
export function sparkPath(values: number[], w: number, h: number): string {
  if (!values || values.length < 2) return "";
  const max = Math.max(...values), min = Math.min(...values);
  const span = max - min || 1;
  const step = w / (values.length - 1);
  return values
    .map((v, i) => `${i ? "L" : "M"}${(i * step).toFixed(1)},${(h - ((v - min) / span) * h).toFixed(1)}`)
    .join(" ");
}

/** Compact "Xs / Xm / Xh ago" from an epoch-seconds timestamp (audit feature A). */
export function agoLabel(epochSec: number, nowSec: number): string {
  const s = Math.max(0, Math.floor(nowSec - epochSec));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ago` : `${Math.floor(m / 60)}h ago`;
}
