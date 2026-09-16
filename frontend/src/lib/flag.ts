// Render a 2-letter ISO-3166 country code as its flag emoji using Unicode regional-indicator
// symbols (no image assets). "US" -> 🇺🇸. Anything that isn't exactly two ASCII letters -> ""
// (so a null/unknown country just shows the IP without a flag).
export function flagEmoji(cc: string | null | undefined): string {
  // Intentional fallback: anything not exactly two ASCII letters yields "" (no glyph);
  // the caller then just renders the raw country code / IP without a flag. No letter badge.
  if (!cc || !/^[A-Za-z]{2}$/.test(cc)) return "";
  const base = 0x1f1e6; // regional indicator 'A'
  const up = cc.toUpperCase();
  return String.fromCodePoint(base + (up.charCodeAt(0) - 65), base + (up.charCodeAt(1) - 65));
}

// One flag of regional indicators at the very start, with the whitespace around it. JS `\s` under the `u` flag also
// covers a no-break space and U+3000. A tag-sequence flag (🏴 + tags) is not an ISO 3166 alpha-2 flag: out of scope.
const LEADING_FLAG = /^\s*([\u{1F1E6}-\u{1F1FF}]{2})\s*/u;

/**
 * A node name's own leading flag, split off so a surface that shows the egress flag never shows two:
 * "🇪🇪 Эстония" → { flag: "🇪🇪", name: "Эстония" }. Only one flag comes off, and only when some name would remain;
 * anything else is returned as { flag: "", name } unchanged.
 */
export function splitLeadingFlag(name: string): { flag: string; name: string } {
  const match = LEADING_FLAG.exec(name);
  const rest = match ? name.slice(match[0].length) : "";
  return match && rest ? { flag: match[1]!, name: rest } : { flag: "", name };
}
