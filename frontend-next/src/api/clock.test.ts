import { afterEach, describe, expect, it, vi } from "vitest";
import { recordServerNow, resetClock, serverNow } from "./clock";

afterEach(() => { resetClock(); vi.useRealTimers(); });

describe("gateway clock", () => {
  it("renders time on the gateway's clock, not the browser's", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(1_700_000_100_000));
    recordServerNow(1_700_000_000);             // the gateway is 100 s behind this browser
    expect(serverNow()).toBe(1_700_000_000_000);
    vi.advanceTimersByTime(5_000);
    expect(serverNow()).toBe(1_700_000_005_000);
  });

  it("ignores a status without server_now and resets to the browser clock", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(1_700_000_100_000));
    recordServerNow(1_700_000_000);
    recordServerNow(undefined);
    expect(serverNow()).toBe(1_700_000_000_000);
    resetClock();
    expect(serverNow()).toBe(Date.now());
  });
});
