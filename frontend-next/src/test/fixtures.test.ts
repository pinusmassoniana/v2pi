import { describe, expect, it } from "vitest";
import { api } from "../api/client";
import { serverNow } from "../api/clock";
import { trafficStore } from "../api/traffic";
import {
  ALL_NODES, ALL_NODE_HEALTH, NETWORK, NODES, NODE_HEALTH, PREVIEW, PREVIEW_NODES, PROFILES, REFRESH_ALL, ROUTING, SETTINGS, STATUS, SUBS,
  TRAFFIC_FRAME, mockApi, mockNodeGroups,
} from "./fixtures";

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

  it("mockApi answers the Nodes screens' reads, and mockNodeGroups serves every group", async () => {
    const api$ = mockApi();
    await expect(api.listProfiles()).resolves.toBe(PROFILES);
    await expect(api.getSettings()).resolves.toBe(SETTINGS);
    await expect(api.previewSub("https://x.example", {})).resolves.toBe(PREVIEW);
    await expect(api.previewSubNodes("https://x.example", {})).resolves.toBe(PREVIEW_NODES);
    await expect(api.refreshAllSubs()).resolves.toBe(REFRESH_ALL);
    mockNodeGroups(api$);
    await expect(api.listNodes()).resolves.toBe(ALL_NODES);
    await expect(api.listNodeHealth()).resolves.toBe(ALL_NODE_HEALTH);
  });

  it("writes answer like the gateway without reaching the network", async () => {
    mockApi();
    await expect(api.putSettings({ tunneled_fetch: false })).resolves.toMatchObject({ tunneled_fetch: false, subs_auto_switch: true });
    await expect(api.probeNode(7)).resolves.toMatchObject({ node_id: 7, last_real_ms: 69 });
    await expect(api.probeNode(99)).resolves.toMatchObject({ node_id: 99, last_real_ms: null });
    await expect(api.addNode({ name: "vps-ams-02", address: "198.51.100.77", port: 443, uuid: "u" })).resolves.toMatchObject({ id: 11, name: "vps-ams-02", subscription_id: null });
    await expect(api.updateNode(2, { note: "n" })).resolves.toMatchObject({ id: 2, name: "de-fra-01", note: "n" });
    await expect(api.addSub({ name: "big-feed", url: "https://feed.example" })).resolves.toMatchObject({ id: 4, name: "big-feed", node_count: 0 });
    await expect(api.updateSub(3, { enabled: true })).resolves.toMatchObject({ id: 3, name: "old", enabled: true });
    await expect(api.validateNode({ name: "a", address: "b", port: 1, uuid: "c" })).resolves.toEqual({ ok: true, error: "" });
  });

  it("node groups: work 6 · home 1 · old 0 · Servers 3, matching each subscription's node count", () => {
    const count = (subscriptionId: number | null) => ALL_NODES.filter((n) => n.subscription_id === subscriptionId).length;
    expect(SUBS.map((s) => [s.name, count(s.id), s.node_count])).toEqual([["work", 6, 6], ["home", 1, 1], ["old", 0, 0]]);
    expect(count(null)).toBe(3);
    expect(new Set(ALL_NODES.map((n) => n.id)).size).toBe(ALL_NODES.length);
    expect(NODES.every((n) => n.subscription_id === 1)).toBe(true);
  });

  it("covers every node state the Nodes screens draw", () => {
    const byId = new Map(ALL_NODE_HEALTH.map((h) => [h.node_id, h]));
    expect(ALL_NODES.filter((n) => n.stale).map((n) => n.name)).toEqual(["us-nyc-01"]);
    expect(ALL_NODES.filter((n) => !byId.has(n.id)).map((n) => n.name)).toEqual(["ch-zrh-02", "kz-ala-01"]);
    expect(ALL_NODE_HEALTH.filter((h) => h.last_real_ok === false).map((h) => h.node_id)).toEqual([6]);
    expect(ALL_NODE_HEALTH.filter((h) => (h.last_http_ms ?? 0) > 150).map((h) => h.node_id)).toEqual([4, 10]);
    expect(new Set(ALL_NODES.map((n) => `${n.transport}·${n.security}`))).toEqual(new Set(["vision·reality", "xhttp·reality", "xhttp·tls"]));
    expect(ALL_NODE_HEALTH.every((h) => ALL_NODES.some((n) => n.id === h.node_id))).toBe(true);
    expect(PROFILES.filter((profile) => profile.is_default).map((profile) => profile.name)).toEqual(["balanced"]);
    expect(SUBS.filter((s) => s.last_error).map((s) => s.name)).toEqual(["home"]);
    expect(SUBS.filter((s) => !s.enabled).map((s) => s.name)).toEqual(["old"]);
  });
});
