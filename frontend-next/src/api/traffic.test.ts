import { describe, expect, it, vi } from "vitest";
import type { TrafficFrame, TrafficHistoryResp, TrafficMessage } from "./client";
import { RING_SIZE, createTrafficStore } from "./traffic";

function fakeConnection() {
  let onMessage: (m: TrafficMessage) => void = () => {};
  let onGap: () => void = () => {};
  const handle = { close: vi.fn(), resume: vi.fn() };
  const connect = vi.fn((m: (m: TrafficMessage) => void, g?: () => void) => {
    onMessage = m;
    onGap = g ?? (() => {});
    return handle;
  });
  return { connect, handle, push: (m: TrafficMessage) => onMessage(m), gap: () => onGap() };
}

const frame = (ts: number, down = 2, up = 1): TrafficFrame => ({
  ts, outbounds: { proxy: { up_bps: up, down_bps: down } }, totals: { up: 0, down: 0 }, active: null,
});
const noHistory = () => Promise.resolve<TrafficHistoryResp>({ samples: [], interval_ms: 1000 });
const flush = () => new Promise((r) => setTimeout(r, 0));

describe("traffic store", () => {
  it("opens one socket for any number of subscribers and closes it after the last leaves", () => {
    const c = fakeConnection();
    const store = createTrafficStore(c.connect, noHistory);
    const a = store.subscribe(() => {});
    const b = store.subscribe(() => {});
    expect(c.connect).toHaveBeenCalledTimes(1);
    a();
    expect(c.handle.close).not.toHaveBeenCalled();
    b();
    expect(c.handle.close).toHaveBeenCalledTimes(1);
  });

  it("appends proxy samples, keeps the last frame, and notifies with a new snapshot", () => {
    const c = fakeConnection();
    const store = createTrafficStore(c.connect, noHistory);
    const listener = vi.fn();
    store.subscribe(listener);
    const before = store.getSnapshot();
    c.push(frame(1000, 50, 5));
    const after = store.getSnapshot();
    expect(after).not.toBe(before);
    expect(after.version).toBe(before.version + 1);
    expect(after.live?.ts).toBe(1000);
    expect(after.samples.at(-1)).toEqual({ ts: 1000, up: 5, down: 50 });
    expect(listener).toHaveBeenCalled();
    expect(store.getSnapshot()).toBe(after);
  });

  it("keeps at most RING_SIZE samples, dropping the oldest", () => {
    const c = fakeConnection();
    const store = createTrafficStore(c.connect, noHistory);
    store.subscribe(() => {});
    for (let i = 0; i < RING_SIZE + 5; i++) c.push(frame(i));
    const { samples } = store.getSnapshot();
    expect(samples).toHaveLength(RING_SIZE);
    expect(samples[0]!.ts).toBe(5);
  });

  it("marks the stream disabled, and ignores transient error frames", () => {
    const c = fakeConnection();
    const store = createTrafficStore(c.connect, noHistory);
    store.subscribe(() => {});
    const v = store.getSnapshot().version;
    c.push({ error: "stats unavailable" });
    expect(store.getSnapshot().version).toBe(v);
    c.push({ disabled: true });
    expect(store.getSnapshot().disabled).toBe(true);
  });

  it("reset closes the socket at once and forgets the frame, the samples and the disabled flag", async () => {
    const c = fakeConnection();
    let answer: (h: TrafficHistoryResp) => void = () => {};
    const loadHistory = vi.fn(() => new Promise<TrafficHistoryResp>((resolve) => { answer = resolve; }));
    const store = createTrafficStore(c.connect, loadHistory);
    store.subscribe(() => {});
    c.push(frame(1000));
    c.push({ disabled: true });
    store.reset();
    expect(c.handle.close).toHaveBeenCalledTimes(1);
    expect(store.getSnapshot()).toMatchObject({ live: null, samples: [], disabled: false });
    // a history reply from the ended session does not refill the window
    answer({ samples: [[900, 1, 1]], interval_ms: 1000 });
    await flush();
    expect(store.getSnapshot().samples).toEqual([]);
    // the next subscriber opens a fresh socket
    store.subscribe(() => {});
    expect(c.connect).toHaveBeenCalledTimes(2);
  });

  it("backfills recorded history on connect and after a gap; live samples win a tie", async () => {
    const c = fakeConnection();
    const loadHistory = vi.fn(() => Promise.resolve<TrafficHistoryResp>({
      samples: [[1000, 9, 9], [2000, 7, 7]], interval_ms: 1000,
    }));
    const store = createTrafficStore(c.connect, loadHistory);
    store.subscribe(() => {});
    c.push(frame(2000, 50, 5));
    await flush();
    expect(store.getSnapshot().samples).toEqual([
      { ts: 1000, up: 9, down: 9 },
      { ts: 2000, up: 5, down: 50 },
    ]);
    c.gap();
    await flush();
    expect(loadHistory).toHaveBeenCalledTimes(2);
  });
});
