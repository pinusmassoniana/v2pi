import { describe, expect, it } from "vitest";
import css from "./index.css?raw";

function block(selector: RegExp): string {
  const match = css.match(selector);
  if (!match) throw new Error(`token block not found: ${selector}`);
  return match[1]!;
}
function token(src: string, name: string): string {
  const match = src.match(new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})`));
  if (!match) throw new Error(`--${name} missing`);
  return match[1]!;
}
function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((c) =>
    c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
}
const contrast = (a: string, b: string) => {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi! + 0.05) / (lo! + 0.05);
};

const themes = {
  dark: block(/:root,\s*\[data-theme="dark"\]\s*\{([^}]*)\}/),
  light: block(/\[data-theme="light"\]\s*\{([^}]*)\}/),
};

describe("design tokens", () => {
  it.each(Object.entries(themes))("%s theme: every text colour meets 4.5:1 on the ground", (_name, src) => {
    for (const text of ["t1", "t2", "t3"]) {
      expect(contrast(token(src, text), token(src, "bg")), `--${text}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it.each(Object.entries(themes))("%s theme: status colours meet 3:1 on the ground", (_name, src) => {
    for (const state of ["ok", "warn", "bad"]) {
      expect(contrast(token(src, state), token(src, "bg")), `--${state}`).toBeGreaterThanOrEqual(3);
    }
  });

  it("defines the same token names in both themes", () => {
    const names = (src: string) => [...src.matchAll(/--([a-z0-9-]+):/g)].map((m) => m[1]).sort();
    expect(names(themes.light)).toEqual(names(themes.dark));
  });
});
