import { describe, expect, it } from "vitest";
import { inIPv4Cidr, parseDestination } from "./routing";

describe("inIPv4Cidr", () => {
  it("matches valid IPv4 CIDRs including /0 and /32", () => {
    expect(inIPv4Cidr("203.0.113.8", "0.0.0.0/0")).toBe(true);
    expect(inIPv4Cidr("203.0.113.8", "203.0.113.8/32")).toBe(true);
    expect(inIPv4Cidr("203.0.113.9", "203.0.113.8/32")).toBe(false);
  });

  it("rejects invalid prefixes instead of applying JS shift modulo rules", () => {
    expect(inIPv4Cidr("10.0.0.1", "10.0.0.0/-1")).toBe(false);
    expect(inIPv4Cidr("10.0.0.1", "10.0.0.0/33")).toBe(false);
    expect(inIPv4Cidr("10.0.0.1", "10.0.0.0/nope")).toBe(false);
  });
});

describe("parseDestination", () => {
  it("parses IPv4/DNS ports and identifies IPv6 as unsupported locally", () => {
    expect(parseDestination("example.com:443")).toEqual({ host: "example.com", port: 443, ipv6: false });
    expect(parseDestination("[2001:db8::1]:443")).toEqual({ host: "2001:db8::1", port: 443, ipv6: true });
    expect(parseDestination("2001:db8::1")).toEqual({ host: "2001:db8::1", port: null, ipv6: true });
  });
});
