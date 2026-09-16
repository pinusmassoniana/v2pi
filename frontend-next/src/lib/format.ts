// Shared number formatters for Home and the traffic chart (one copy, not one per component — audit E3).

/** Bits-per-second → "1.2 Mbit/s" / "340 kbit/s" / "12 bit/s". */
export function fmtRate(bps: number): string {
  if (!Number.isFinite(bps)) return "—";   // partial WS frame / undefined → dash, not "NaN bit/s"
  if (bps >= 1e6) return (bps / 1e6).toFixed(1) + " Mbit/s";
  if (bps >= 1e3) return (bps / 1e3).toFixed(0) + " kbit/s";
  return Math.round(bps) + " bit/s";
}

/** Bytes → "1.23 GB" / "4.5 MB" / "678 KB" / "9 B". */
export function fmtBytes(b: number): string {
  if (!Number.isFinite(b)) return "—";
  if (b >= 1e9) return (b / 1e9).toFixed(2) + " GB";
  if (b >= 1e6) return (b / 1e6).toFixed(1) + " MB";
  if (b >= 1e3) return (b / 1e3).toFixed(0) + " KB";
  return Math.round(b) + " B";
}

/** Host syntax for URI authorities: literal IPv6 must be bracketed. */
export function formatUriHost(host: string): string {
  if (host.startsWith("[") && host.endsWith("]")) return host;
  return host.includes(":") ? `[${host}]` : host;
}

/** Seconds → "3d 04:12:09" (days only when there are any): the live uptime that ticks every second. */
export function fmtUptime(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const days = Math.floor(s / 86_400);
  const clock = [Math.floor((s % 86_400) / 3_600), Math.floor((s % 3_600) / 60), s % 60]
    .map((n) => String(n).padStart(2, "0"))
    .join(":");
  return days > 0 ? `${days}d ${clock}` : clock;
}

/** Seconds → "3d 4h" / "4h 12m" / "12m": an uptime read at a glance. */
export function fmtUptimeCoarse(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const days = Math.floor(s / 86_400);
  const hours = Math.floor((s % 86_400) / 3_600);
  const minutes = Math.floor((s % 3_600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  return hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m`;
}

/** "12.4 Mbit/s" → { value: "12.4", unit: "Mbit/s" }, so a KPI can set the unit smaller than the number. */
export function splitUnit(text: string): { value: string; unit: string } {
  const at = text.lastIndexOf(" ");
  return at < 0 ? { value: text, unit: "" } : { value: text.slice(0, at), unit: text.slice(at + 1) };
}
