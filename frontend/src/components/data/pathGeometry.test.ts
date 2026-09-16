import { describe, expect, it } from "vitest";
import { PATH_GEOMETRY, lineBaseline, pillHeight, pillWidth } from "./pathGeometry";

const LAYOUTS = Object.entries(PATH_GEOMETRY);

describe("connection path geometry", () => {
  it.each(LAYOUTS)("%s: the stats grid's column centres sit under the four points", (_layout, g) => {
    // grid-cols-4: column i is centred at (2i + 1) / 8 of the card, which is where point i must be drawn
    g.xs.forEach((x, i) => expect(x).toBeCloseTo(((2 * i + 1) / 8) * g.width, 0));
  });

  it.each(LAYOUTS)("%s: the direct arc runs from above the gateway to above the internet, and its flow runs it back", (_layout, g) => {
    expect(g.direct).toMatch(new RegExp(`^M${g.xs[1]},\\d+ .* ${g.xs[3]},\\d+$`));
    expect(g.directBack).toMatch(new RegExp(`^M${g.xs[3]},\\d+ .* ${g.xs[1]},\\d+$`));
    expect(g.directBack.startsWith(`M${g.direct.split(" ").at(-1)}`)).toBe(true);
  });

  it("a pill is its longest line plus one character of mono advance and its padding, and grows a gap per line", () => {
    const { wide, narrow } = PATH_GEOMETRY;
    expect(pillWidth(["idle"], wide.directPill)).toBe(46);
    expect(pillWidth(["↓ 12.4 Mbit/s", "↑ 1.8 Mbit/s"], narrow.tunnelPill)).toBeCloseTo(85.6, 5);
    expect([pillHeight(wide.directPill, 1), pillHeight(wide.tunnelPill, 2), pillHeight(narrow.directPill, 2), pillHeight(narrow.tunnelPill, 1)])
      .toEqual([18, 30, 28, 17]);
  });

  it("wide: the worst realistic tunnel pill fits between the gateway lock and the node marker", () => {
    const g = PATH_GEOMETRY.wide;
    const worst = pillWidth(["↓ 1000.0 Mbit/s", "↑ 1000.0 Mbit/s"], g.tunnelPill);
    expect(worst).toBe(108);
    expect(g.tunnelPill.cx - worst / 2).toBeGreaterThanOrEqual(g.xs[1] + g.marker.lock);
    expect(g.tunnelPill.cx + worst / 2).toBeLessThanOrEqual(g.xs[2] - g.marker.node);
  });

  it("narrow: the tunnel pill hangs below the line, clear of the node marker's ring, inside the drawing", () => {
    const g = PATH_GEOMETRY.narrow;
    const height = pillHeight(g.tunnelPill, 2);
    const top = g.tunnelPill.cy - height / 2;
    expect(top).toBe(93);
    // A stroke is centred on its edge: the pill's 1 u hairline reaches 0.5 u outside it, the node ring's 2 u stroke 1 u past its radius.
    // A rate line of 12 characters or more widens the pill under the node marker, so this clearance is what keeps them apart.
    expect(top - 0.5).toBeGreaterThanOrEqual(g.y + g.marker.node + 1);
    expect(top + height + 0.5).toBeLessThanOrEqual(g.height);
    expect(g.tunnelPill.cx).toBe((g.xs[1] + g.xs[2]) / 2);
  });

  it("lines are centred on their pill, one em-fraction below the middle for the baseline", () => {
    const { wide, narrow } = PATH_GEOMETRY;
    expect(lineBaseline(wide.directPill, 0, 1)).toBe(29.5);
    expect(lineBaseline(wide.tunnelPill, 0, 2)).toBeCloseTo(69.5, 5);
    expect(lineBaseline(wide.tunnelPill, 1, 2)).toBeCloseTo(81.5, 5);
    const mid = (lineBaseline(narrow.tunnelPill, 0, 2) + lineBaseline(narrow.tunnelPill, 1, 2)) / 2;
    expect(mid - 0.35 * narrow.tunnelPill.size).toBeCloseTo(narrow.tunnelPill.cy, 5);
  });
});
