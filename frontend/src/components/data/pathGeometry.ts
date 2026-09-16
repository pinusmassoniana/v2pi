// The connection path's two drawings, with the approved mockup's numbers: "wide" for a card at least 28 rem across,
// "narrow" below that. Pure numbers and arithmetic, unit-tested without rendering; ConnectionPath draws them.

/** `font-mono` is IBM Plex Mono, which advances 0.6 em per character. */
export const MONO_ADVANCE = 0.6;

export type PathLayout = "wide" | "narrow";

export interface PillBox {
  cx: number;
  cy: number;
  /** Height with one line; each further line adds `gap`. */
  height: number;
  rx: number;
  /** Mono font size of its lines. */
  size: number;
  padX: number;
  /** Distance between two lines' middles. */
  gap: number;
}

export interface PathGeometry {
  width: number;
  height: number;
  /** x of devices, gateway, node and internet: the centres of the stats grid's four columns. */
  xs: readonly [number, number, number, number];
  /** y of the line the four points sit on. */
  y: number;
  /** The direct line, from above the gateway over to above the internet. */
  direct: string;
  /** The same arc from the internet back to the gateway: the direction its flow runs (download). */
  directBack: string;
  directLabel: { x: number; y: number; size: number };
  /** Direct rates: both directions on one line wide, one line each narrow. */
  directPill: PillBox & { lines: 1 | 2 };
  /** Tunnel rates, one line each way: on the gateway → node leg wide, hanging below it narrow. */
  tunnelPill: PillBox;
  /** The ✗ that replaces the tunnel pill while the real check fails. */
  badge: { cx: number; cy: number; r: number };
  /** Radii: the devices and internet dot and its ring, the gateway lock, the node marker, and the flag's font size. */
  marker: { dot: number; ring: number; lock: number; node: number; flag: number };
}

export const PATH_GEOMETRY: Record<PathLayout, PathGeometry> = {
  wide: {
    width: 568,
    height: 98,
    xs: [71, 213, 355, 497],
    y: 72,
    direct: "M213,64 C213,30 232,26 262,26 L448,26 C478,26 497,30 497,64",
    directBack: "M497,64 C497,30 478,26 448,26 L262,26 C232,26 213,30 213,64",
    directLabel: { x: 355, y: 11, size: 10 },
    directPill: { cx: 355, cy: 26, height: 18, rx: 9, size: 10, padX: 8, gap: 12, lines: 1 },
    tunnelPill: { cx: 284, cy: 72, height: 18, rx: 9, size: 10, padX: 6, gap: 12 },
    badge: { cx: 284, cy: 72, r: 9.5 },
    marker: { dot: 5.5, ring: 10, lock: 10.5, node: 14, flag: 13 },
  },
  narrow: {
    width: 334,
    height: 122,
    xs: [42, 125, 209, 292],
    y: 78,
    direct: "M125,70 C125,36 140,30 162,30 L255,30 C277,30 292,36 292,70",
    directBack: "M292,70 C292,36 277,30 255,30 L162,30 C140,30 125,36 125,70",
    directLabel: { x: 209, y: 11, size: 9.5 },
    directPill: { cx: 209, cy: 30, height: 17, rx: 8.5, size: 9, padX: 5, gap: 11, lines: 2 },
    tunnelPill: { cx: 167, cy: 107, height: 17, rx: 8.5, size: 9, padX: 5, gap: 11 },
    badge: { cx: 167, cy: 78, r: 8.5 },
    marker: { dot: 5, ring: 9, lock: 10, node: 12, flag: 11 },
  },
};

/** A pill's width: its longest line plus one character (an arrow may fall back to a wider face), and its padding. */
export function pillWidth(lines: readonly string[], pill: Pick<PillBox, "size" | "padX">): number {
  const chars = Math.max(0, ...lines.map((line) => [...line].length));
  return (chars + 1) * MONO_ADVANCE * pill.size + 2 * pill.padX;
}

/** A pill's height with `lines` lines. */
export function pillHeight(pill: Pick<PillBox, "height" | "gap">, lines: number): number {
  return pill.height + (Math.max(1, lines) - 1) * pill.gap;
}

/** The baseline of line `index` of `count`, the lines centred on the pill: a line's middle sits 0.35 em above its baseline. */
export function lineBaseline(pill: PillBox, index: number, count: number): number {
  return pill.cy + (index - (count - 1) / 2) * pill.gap + 0.35 * pill.size;
}
