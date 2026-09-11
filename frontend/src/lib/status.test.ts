import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type Status } from "./api";
import { pollStatusOnce, resetStatus, statusStore, subscribeStatus } from "./status.svelte";

const STATUS: Status = {
  running: true, pid: 1, active_node_id: 2, xray_state: "working", active_since: null,
  last_failover_at: null, prev_active_node_id: null, server_now: 1_700_000_000,
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

beforeEach(() => {
  resetStatus();
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
});

afterEach(() => {
  resetStatus();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("shared status coordinator", () => {
  it("deduplicates an in-flight status request", async () => {
    const d = deferred<Status>();
    const get = vi.spyOn(api, "getStatus").mockReturnValue(d.promise);
    const a = pollStatusOnce();
    const b = pollStatusOnce();
    expect(get).toHaveBeenCalledTimes(1);
    d.resolve(STATUS);
    await Promise.all([a, b]);
    expect(statusStore.value?.pid).toBe(1);
  });

  it("does not let a response from a reset session repopulate status", async () => {
    const d = deferred<Status>();
    vi.spyOn(api, "getStatus").mockReturnValue(d.promise);
    const pending = pollStatusOnce();
    resetStatus();
    d.resolve(STATUS);
    await pending;
    expect(statusStore.value).toBeNull();
  });

  it("does not let a second subscriber delay the first poll", async () => {
    // The shell subscribes, then the screen it renders subscribes too. _retime() used to clear the
    // pending immediate poll and reschedule it a full interval out, so the panel started with no
    // status at all for 3 s — and an unreachable panel took just as long to admit it.
    const get = vi.spyOn(api, "getStatus").mockResolvedValue(STATUS);
    const stopShell = subscribeStatus(4000);
    const stopScreen = subscribeStatus(3000);          // mounts right behind it
    await new Promise((resolve) => setTimeout(resolve, 5));
    expect(get).toHaveBeenCalledTimes(1);
    stopScreen(); stopShell();
  });

  it("records when the last poll actually succeeded, and forgets it on reset", async () => {
    vi.spyOn(api, "getStatus").mockResolvedValue(STATUS);
    expect(statusStore.lastOkAt).toBeNull();
    await pollStatusOnce();
    expect(statusStore.lastOkAt).toBeGreaterThan(0);
    expect(statusStore.stale).toBe(false);
    resetStatus();
    expect(statusStore.lastOkAt).toBeNull();
  });

  it("schedules the next poll only after the current one finishes", async () => {
    vi.useFakeTimers();
    const first = deferred<Status>();
    const get = vi.spyOn(api, "getStatus")
      .mockReturnValueOnce(first.promise)
      .mockResolvedValue(STATUS);
    const stop = subscribeStatus(100);
    await vi.advanceTimersByTimeAsync(500);
    expect(get).toHaveBeenCalledTimes(1);
    first.resolve(STATUS);
    await Promise.resolve();
    await vi.advanceTimersByTimeAsync(99);
    expect(get).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(get).toHaveBeenCalledTimes(2);
    stop();
  });
});
