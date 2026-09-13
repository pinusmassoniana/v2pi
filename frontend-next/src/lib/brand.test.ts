import { describe, it, expect } from "vitest";
import { BRAND } from "./brand";

describe("brand", () => {
  it("is v2pi", () => {
    expect(BRAND).toBe("v2pi");
  });
});
