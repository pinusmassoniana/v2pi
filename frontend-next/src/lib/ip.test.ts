import { describe, expect, it } from "vitest";
import { ipv4Number, ipv4Span, ipv6PrefixLength, isIPv6Address, isIPv6Network } from "./ip";

describe("IP parsing, as Python's ipaddress reads it", () => {
  it("IPv4: dotted quads without leading zeros", () => {
    expect(ipv4Number("192.168.50.1")).toBe(3_232_248_321);
    expect(ipv4Number("0.0.0.0")).toBe(0);
    for (const bad of ["192.168.050.1", "1.2.3", "1.2.3.4.5", "256.0.0.1", " 1.2.3.4", ""]) expect(ipv4Number(bad)).toBeNull();
    expect(ipv4Span("192.168.50.77/24")).toEqual({ first: 3_232_248_320, last: 3_232_248_575 });
    expect(ipv4Span("192.168.50.1/33")).toBeNull();
    expect(ipv4Span("8.8.8.8/")).toBeNull();
  });

  it("IPv6: groups, one ::, an IPv4 tail; a prefix length 0–128, 128 without one", () => {
    for (const address of ["::", "::1", "2606:4700:4700::1111", "1:2:3:4:5:6:7:8", "::ffff:192.168.1.1", "fe80::"]) expect(isIPv6Address(address)).toBe(true);
    for (const bad of ["", "1.2.3.4", "1::2::3", "12345::", "1:2:3:4:5:6:7:8:9", "gggg::", ":1"]) expect(isIPv6Address(bad)).toBe(false);
    expect(ipv6PrefixLength("2001:db8::/64")).toBe(64);
    expect(ipv6PrefixLength("2001:db8::1")).toBe(128);
    expect(ipv6PrefixLength("2001:db8::/129")).toBeNull();
    expect(ipv6PrefixLength("2001:db8::/6x")).toBeNull();
    expect(ipv6PrefixLength("10.0.0.0/8")).toBeNull();
    expect(isIPv6Network("1:2:3:4:5:6:7::")).toBe(true);
    expect(isIPv6Network("gggg::1")).toBe(false);
  });
});
