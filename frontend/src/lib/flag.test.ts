import { describe, it, expect } from "vitest";
import { flagEmoji } from "./flag";

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
