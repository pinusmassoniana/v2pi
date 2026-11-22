import { describe, expect, it } from "vitest";
import { cn } from "./cn";

describe("cn", () => {
  it("keeps a ramp size next to a text colour, and lets a later size win", () => {
    expect(cn("text-caption text-t3")).toBe("text-caption text-t3");
    expect(cn("text-2xs", "text-body")).toBe("text-body");
  });
});
