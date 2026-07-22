import { describe, expect, it } from "vitest";
import { formatUriHost } from "./format";

describe("formatUriHost", () => {
  it("brackets literal IPv6 hosts exactly once", () => {
    expect(formatUriHost("2001:db8::1")).toBe("[2001:db8::1]");
    expect(formatUriHost("[2001:db8::1]")).toBe("[2001:db8::1]");
  });

  it("leaves DNS and IPv4 hosts unchanged", () => {
    expect(formatUriHost("vpn.example.com")).toBe("vpn.example.com");
    expect(formatUriHost("192.0.2.7")).toBe("192.0.2.7");
  });
});
