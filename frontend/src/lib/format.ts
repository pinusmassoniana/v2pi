// Shared number formatters for the Dashboard / TrafficGraph (kept out of the .svelte files so
// they're not duplicated — audit E3).

/** Bits-per-second → "1.2 Mbit/s" / "340 kbit/s" / "12 bit/s". */
export function fmtRate(bps: number): string {
  if (bps >= 1e6) return (bps / 1e6).toFixed(1) + " Mbit/s";
  if (bps >= 1e3) return (bps / 1e3).toFixed(0) + " kbit/s";
  return Math.round(bps) + " bit/s";
}

/** Bytes → "1.23 GB" / "4.5 MB" / "678 KB" / "9 B". */
export function fmtBytes(b: number): string {
  if (b >= 1e9) return (b / 1e9).toFixed(2) + " GB";
  if (b >= 1e6) return (b / 1e6).toFixed(1) + " MB";
  if (b >= 1e3) return (b / 1e3).toFixed(0) + " KB";
  return Math.round(b) + " B";
}
