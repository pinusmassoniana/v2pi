import { describe, expect, it } from "vitest";
import { fmtUptime, fmtUptimeCoarse, formatUriHost, splitUnit } from "./format";

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

describe("uptime", () => {
  it("ticks as HH:MM:SS and adds days only once there are any", () => {
    expect(fmtUptime(0)).toBe("00:00:00");
    expect(fmtUptime(65)).toBe("00:01:05");
    expect(fmtUptime(86_399)).toBe("23:59:59");
    expect(fmtUptime(3 * 86_400 + 4 * 3_600 + 12 * 60 + 9)).toBe("3d 04:12:09");
  });

  it("never goes negative and ignores fractions", () => {
    expect(fmtUptime(-30)).toBe("00:00:00");
    expect(fmtUptime(59.9)).toBe("00:00:59");
  });

  it("coarse: the two largest units", () => {
    expect(fmtUptimeCoarse(3 * 86_400 + 4 * 3_600 + 50 * 60)).toBe("3d 4h");
    expect(fmtUptimeCoarse(4 * 3_600 + 12 * 60)).toBe("4h 12m");
    expect(fmtUptimeCoarse(12 * 60 + 59)).toBe("12m");
    expect(fmtUptimeCoarse(-5)).toBe("0m");
  });
});

describe("splitUnit", () => {
  it("splits a formatted value at its last space", () => {
    expect(splitUnit("12.4 Mbit/s")).toEqual({ value: "12.4", unit: "Mbit/s" });
    expect(splitUnit("18.60 GB")).toEqual({ value: "18.60", unit: "GB" });
  });

  it("leaves a bare value alone", () => {
    expect(splitUnit("—")).toEqual({ value: "—", unit: "" });
  });
});
