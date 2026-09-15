import { describe, expect, it } from "vitest";
import { GATEWAY_NETWORK, NOW_SEC } from "../../test/fixtures";
import { leaseKey, leaseLeft, sortLeases } from "./leases";

const NOW_MS = NOW_SEC * 1000;
const lease = (ip: string, mac = `aa:${ip}`) => ({ ip, mac, hostname: "", expiry: 0 });

describe("DHCP leases", () => {
  it("sorts by address as numbers, not text, and keeps the input untouched", () => {
    const leases = [lease("192.168.50.10"), lease("192.168.50.9"), lease("192.168.50.100"), lease("10.0.0.2")];
    expect(sortLeases(leases).map((row) => row.ip)).toEqual(["10.0.0.2", "192.168.50.9", "192.168.50.10", "192.168.50.100"]);
    expect(leases[0]!.ip).toBe("192.168.50.10");
    expect(sortLeases(GATEWAY_NETWORK.status.clients).map((row) => row.ip.split(".")[3])).toEqual(["101", "104", "112", "118", "123", "140", "176"]);
  });

  it("keys a lease by device and address", () => {
    expect(leaseKey({ ip: "192.168.50.101", mac: "aa:bb:cc:00:01:01", hostname: "iphone-anna", expiry: 1 })).toBe("aa:bb:cc:00:01:01|192.168.50.101");
  });

  it("time left, floored to minutes, hours or days; 0 never expires", () => {
    expect(leaseLeft(0, NOW_MS)).toBe("no expiry");
    expect(leaseLeft(NOW_SEC + 38 * 60 + 59, NOW_MS)).toBe("38m left");
    expect(leaseLeft(NOW_SEC + 59, NOW_MS)).toBe("0m left");
    expect(leaseLeft(NOW_SEC + 3_599, NOW_MS)).toBe("59m left");
    expect(leaseLeft(NOW_SEC + 3_600, NOW_MS)).toBe("1h left");
    expect(leaseLeft(NOW_SEC + 11 * 3_600 + 3_599, NOW_MS)).toBe("11h left");
    expect(leaseLeft(NOW_SEC + 86_400, NOW_MS)).toBe("1d left");
    expect(leaseLeft(NOW_SEC + 2 * 86_400 + 86_399, NOW_MS)).toBe("2d left");
    expect(leaseLeft(NOW_SEC - 30, NOW_MS)).toBe("0m left");
  });
});
