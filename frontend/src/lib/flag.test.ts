import { describe, it, expect } from "vitest";
import { flagEmoji, splitLeadingFlag } from "./flag";

describe("flagEmoji", () => {
  it("maps a 2-letter code to its flag emoji", () => {
    expect(flagEmoji("US")).toBe("🇺🇸");
    expect(flagEmoji("NL")).toBe("🇳🇱");
  });

  it("is case-insensitive", () => {
    expect(flagEmoji("de")).toBe("🇩🇪");
  });

  it("returns empty string for null/invalid codes", () => {
    expect(flagEmoji(null)).toBe("");
    expect(flagEmoji(undefined)).toBe("");
    expect(flagEmoji("")).toBe("");
    expect(flagEmoji("USA")).toBe("");
    expect(flagEmoji("1")).toBe("");
    expect(flagEmoji("U1")).toBe("");
  });
});

describe("splitLeadingFlag", () => {
  it.each([
    ["a flag, a space and the name", "🇪🇪 Эстония", "🇪🇪", "Эстония"],
    ["a flag glued to the name", "🇪🇪Эстония", "🇪🇪", "Эстония"],
    ["a no-break space after the flag", "🇪🇪 Эстония", "🇪🇪", "Эстония"],
    ["an ideographic space after the flag", "🇪🇪　Эстония", "🇪🇪", "Эстония"],
    ["spaces around the flag", "  🇳🇱   nl-ams-03", "🇳🇱", "nl-ams-03"],
    ["two flags: only the first comes off", "🇪🇪🇫🇮 Tallinn", "🇪🇪", "🇫🇮 Tallinn"],
  ])("splits %s", (_what, input, flag, name) => {
    expect(splitLeadingFlag(input)).toEqual({ flag, name });
  });

  it.each([
    ["a name without a flag", "nl-ams-03"],
    ["No node", "No node"],
    ["a node not listed yet", "node #4"],
    ["a flag that does not lead", "Эстония 🇪🇪"],
    ["a flag with nothing after it", "🇪🇪"],
    ["a flag followed only by spaces", "🇪🇪  "],
    ["a lone regional indicator", "\u{1F1EA} Estonia"],
    ["a tag-sequence flag", "\u{1F3F4}\u{E0067}\u{E0062}\u{E0073}\u{E0063}\u{E0074}\u{E007F} Scotland"],
  ])("leaves %s unchanged", (_what, input) => {
    expect(splitLeadingFlag(input)).toEqual({ flag: "", name: input });
  });
});
