import { describe, expect, it } from "vitest";
import { api } from "../api/client";
import { serverNow } from "../api/clock";
import { trafficStore } from "../api/traffic";
import { NETWORK, NODES, NODE_HEALTH, ROUTING, STATUS, SUBS, TRAFFIC_FRAME, mockApi } from "./fixtures";

describe("gateway fixtures", () => {
  it("mockApi answers every read the Home screens make", async () => {
    mockApi();
    await expect(api.getStatus()).resolves.toBe(STATUS);
    await expect(api.listNodes()).resolves.toBe(NODES);
    await expect(api.getNetwork()).resolves.toBe(NETWORK);
    await expect(api.listNodeHealth()).resolves.toBe(NODE_HEALTH);
    await expect(api.listSubs()).resolves.toBe(SUBS);
    await expect(api.getRouting()).resolves.toBe(ROUTING);
  });

  it("the shell's two named nodes keep their ids, so existing screens and tests still find them", () => {
    expect(NODES.slice(0, 2).map((n) => [n.id, n.name])).toEqual([[1, "nl-ams-03"], [2, "de-fra-01"]]);
    expect(new Set(NODES.map((n) => n.id)).size).toBe(NODES.length);
  });

  it("is internally consistent: one gateway, one clock, one active node", () => {
    expect(STATUS.active_node_id).toBe(1);
    expect(TRAFFIC_FRAME.active?.node_id).toBe(STATUS.active_node_id);
    expect(TRAFFIC_FRAME.ts).toBe(STATUS.server_now * 1000);
    // every health row belongs to a node, and one node has never been probed
    const ids = new Set(NODES.map((n) => n.id));
    expect(NODE_HEALTH.every((h) => ids.has(h.node_id))).toBe(true);
    expect(NODES.some((n) => !NODE_HEALTH.some((h) => h.node_id === n.id))).toBe(true);
    // events arrive oldest first, as the backend appends them, and there are more than the six Overview shows
    const ts = NETWORK.events.map((e) => e.ts);
    expect([...ts].sort((a, b) => a - b)).toEqual(ts);
    expect(NETWORK.events.length).toBeGreaterThan(6);
    expect(ts.every((t) => t <= STATUS.server_now)).toBe(true);
  });

  it("starts each test on the gateway clock and with an empty traffic store", () => {
    const previous = mockApi();
    const unsubscribe = trafficStore.subscribe(() => {});
    previous.emitTraffic(TRAFFIC_FRAME);
    unsubscribe();
    mockApi();
    expect(Math.abs(serverNow() - STATUS.server_now * 1000)).toBeLessThan(1_000);
    expect(trafficStore.getSnapshot()).toMatchObject({ live: null, disabled: false });
    expect(trafficStore.getSnapshot().samples).toHaveLength(0);
  });

  it("emitTraffic delivers a frame to the live store once something subscribes", () => {
    const api$ = mockApi();
    expect(() => api$.emitTraffic(TRAFFIC_FRAME)).toThrow(/no traffic subscriber/);
    const unsubscribe = trafficStore.subscribe(() => {});
    api$.emitTraffic(TRAFFIC_FRAME);
    expect(trafficStore.getSnapshot().live).toBe(TRAFFIC_FRAME);
    api$.emitTraffic({ disabled: true });
    expect(trafficStore.getSnapshot().disabled).toBe(true);
    unsubscribe();
  });
});
