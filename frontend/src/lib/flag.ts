// Render a 2-letter ISO-3166 country code as its flag emoji using Unicode regional-indicator
// symbols (no image assets). "US" -> 🇺🇸. Anything that isn't exactly two ASCII letters -> ""
// (so a null/unknown country just shows the IP without a flag).
export function flagEmoji(cc: string | null | undefined): string {
  if (!cc || !/^[A-Za-z]{2}$/.test(cc)) return "";
  const base = 0x1f1e6; // regional indicator 'A'
  const up = cc.toUpperCase();
  return String.fromCodePoint(base + (up.charCodeAt(0) - 65), base + (up.charCodeAt(1) - 65));
}
