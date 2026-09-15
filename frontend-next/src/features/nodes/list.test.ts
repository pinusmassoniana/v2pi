import { describe, expect, it } from "vitest";
import type { Node } from "../../api/client";
import { ALL_NODES, ALL_NODE_HEALTH, NODES, NODE_HEALTH, NOW_SEC, SUBS, TRAFFIC_FRAME, node } from "../../test/fixtures";
import {
  ROW_CAP, SERVERS, applyOrder, bestReading, bestScope, canReorder, checkedAgo, defaultGroup, deleteNodeMessage, groupChips, groupOf, healthById, inScope,
  liveReading, matchesSearch, mergeHealth, moveWithin, parseGroup, pillTone, probeScope, pruneSelection, resolveGroup, selectionState, shownNodes,
  sortNodes, visibleRows,
} from "./list";

const HEALTH = healthById(ALL_NODE_HEALTH);
const names = (nodes: readonly Node[]) => nodes.map((n) => n.name);

describe("groups", () => {
  it("default: the first subscription, else Servers", () => {
    expect(defaultGroup(SUBS)).toBe(1);
    expect(defaultGroup([])).toBe(SERVERS);
  });

  it("parses the URL's group: servers, a positive integer id, or nothing", () => {
    expect(parseGroup("servers")).toBe(SERVERS);
    expect(parseGroup(2)).toBe(2);
    expect(parseGroup("2")).toBe(2);
    expect(parseGroup("0")).toBeNull();
    expect(parseGroup(-1)).toBeNull();
    expect(parseGroup(1.5)).toBeNull();
    expect(parseGroup("work")).toBeNull();
    expect(parseGroup(undefined)).toBeNull();
  });

  it("resolves a known group as is, and an unknown or vanished one to the default with a replace", () => {
    expect(resolveGroup("servers", SUBS)).toEqual({ group: SERVERS, replace: false });
    expect(resolveGroup(2, SUBS)).toEqual({ group: 2, replace: false });
    expect(resolveGroup(undefined, SUBS)).toEqual({ group: 1, replace: false });
    expect(resolveGroup(9, SUBS)).toEqual({ group: 1, replace: true });
    expect(resolveGroup("nonsense", SUBS)).toEqual({ group: 1, replace: true });
    expect(resolveGroup(9, [])).toEqual({ group: SERVERS, replace: true });
  });

  it("takes a subscription id on trust until the subscriptions load", () => {
    expect(resolveGroup(9, undefined)).toEqual({ group: 9, replace: false });
    expect(resolveGroup(undefined, undefined)).toEqual({ group: SERVERS, replace: false });
  });

  it("scopes nodes to their group, and names each group's probe and connect-best scope", () => {
    expect(ALL_NODES.filter((n) => inScope(n, SERVERS)).map((n) => n.name)).toEqual(["vps-hel", "lab-lan", "kz-ala-01"]);
    expect(ALL_NODES.filter((n) => inScope(n, 2)).map((n) => n.name)).toEqual(["us-nyc-01"]);
    expect(groupOf(ALL_NODES[0]!)).toBe(1);
    expect(groupOf(ALL_NODES[6]!)).toBe(SERVERS);
    expect([probeScope(SERVERS), probeScope(2)]).toEqual(["servers", "2"]);
    expect([bestScope(SERVERS), bestScope(2)]).toEqual([null, 2]);
  });

  it("chips: every subscription in order with its node count, then Servers", () => {
    expect(groupChips(ALL_NODES, SUBS)).toEqual([
      { key: 1, label: "work", count: 6 }, { key: 2, label: "home", count: 1 }, { key: 3, label: "old", count: 0 },
      { key: SERVERS, label: "Servers", count: 3 },
    ]);
    expect(groupChips([], [])).toEqual([{ key: SERVERS, label: "Servers", count: 0 }]);
  });
});

describe("search and sort", () => {
  it("searches name, address and note, ignoring case and surrounding space", () => {
    expect(matchesSearch(NODES[1]!, "STREAMING")).toBe(true);
    expect(matchesSearch(NODES[1]!, " de-fra ")).toBe(true);
    expect(matchesSearch(NODES[1]!, "example.org")).toBe(true);
    expect(matchesSearch(NODES[1]!, "hel")).toBe(false);
    expect(matchesSearch(NODES[1]!, "   ")).toBe(true);
  });

  it("pos keeps the backend order; name and address compare by locale", () => {
    expect(names(sortNodes(NODES, HEALTH, "pos", "desc"))).toEqual(names(NODES));
    expect(names(sortNodes(NODES, HEALTH, "name", "asc"))).toEqual(["ch-zrh-02", "de-fra-01", "fi-hel-02", "nl-ams-03", "pl-waw-01", "se-sto-01"]);
    expect(names(sortNodes(NODES, HEALTH, "name", "desc"))[0]).toBe("se-sto-01");
    expect(names(sortNodes(ALL_NODES.filter((n) => inScope(n, SERVERS)), HEALTH, "address", "asc"))).toEqual(["lab-lan", "vps-hel", "kz-ala-01"]);
  });

  it("latency sorts put a node without a reading last ascending and first descending", () => {
    expect(names(sortNodes(NODES, HEALTH, "tcp", "asc"))).toEqual(["nl-ams-03", "de-fra-01", "fi-hel-02", "se-sto-01", "pl-waw-01", "ch-zrh-02"]);
    expect(names(sortNodes(NODES, HEALTH, "tcp", "desc"))).toEqual(["ch-zrh-02", "pl-waw-01", "se-sto-01", "fi-hel-02", "de-fra-01", "nl-ams-03"]);
    expect(names(sortNodes(NODES, HEALTH, "http", "asc"))).toEqual(["de-fra-01", "fi-hel-02", "se-sto-01", "nl-ams-03", "pl-waw-01", "ch-zrh-02"]);
    const unprobed = [node(21, "b"), node(20, "a")];
    expect(names(sortNodes(unprobed, HEALTH, "http", "asc"))).toEqual(["b", "a"]);   // two unknowns keep their order
  });

  it("shown rows: the group, searched, then sorted — every row, not only the rendered ones", () => {
    expect(names(shownNodes(ALL_NODES, HEALTH, 1, "", "pos", "asc"))).toHaveLength(6);
    expect(names(shownNodes(ALL_NODES, HEALTH, 1, "-0", "name", "desc"))).toEqual(["se-sto-01", "pl-waw-01", "nl-ams-03", "fi-hel-02", "de-fra-01", "ch-zrh-02"]);
    expect(names(shownNodes(ALL_NODES, HEALTH, SERVERS, "own", "pos", "asc"))).toEqual(["vps-hel"]);
  });
});

describe("reorder, cap and selection", () => {
  it("reorders only the Servers group in position order with no search", () => {
    expect(canReorder(SERVERS, "pos", "")).toBe(true);
    expect(canReorder(SERVERS, "pos", "  ")).toBe(true);
    expect(canReorder(SERVERS, "name", "")).toBe(false);
    expect(canReorder(SERVERS, "pos", "vps")).toBe(false);
    expect(canReorder(1, "pos", "")).toBe(false);
  });

  it("applies a new order to one group's nodes and leaves every other node in its place", () => {
    const order = applyOrder(ALL_NODES, [8, 7, 9]);
    expect(order.map((n) => n.id)).toEqual([1, 2, 3, 4, 5, 6, 8, 7, 9, 10]);
    expect(applyOrder(ALL_NODES, [])).toEqual(ALL_NODES);
    expect(applyOrder(ALL_NODES, [99, 9]).map((n) => n.id)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
  });

  it("moves a row by swapping it with its neighbour and returns the whole list", () => {
    expect(moveWithin([7, 8, 9], 0, 1)).toEqual([8, 7, 9]);
    expect(moveWithin([7, 8, 9], 2, -1)).toEqual([7, 9, 8]);
    expect(moveWithin([7, 8, 9], 0, -1)).toBeNull();
    expect(moveWithin([7, 8, 9], 2, 1)).toBeNull();
    expect(moveWithin([7, 8, 9], 5, -1)).toBeNull();
  });

  it("renders the first 100 rows unless all are asked for", () => {
    const rows = Array.from({ length: 240 }, (_, i) => i);
    expect(ROW_CAP).toBe(100);
    expect(visibleRows(rows, false)).toHaveLength(100);
    expect(visibleRows(rows, true)).toBe(rows);
    const few = rows.slice(0, 100);
    expect(visibleRows(few, false)).toBe(few);
  });

  it("prunes the selection to shown rows, keeping the same set when nothing changed", () => {
    const selected = new Set([1, 2]);
    expect(pruneSelection(selected, NODES)).toBe(selected);
    expect([...pruneSelection(new Set([1, 7, 2]), NODES)]).toEqual([1, 2]);
    expect(pruneSelection(new Set([7]), NODES).size).toBe(0);
  });

  it("select-all is none, some or all of the shown rows", () => {
    expect(selectionState(new Set(), NODES)).toBe("none");
    expect(selectionState(new Set([1, 7]), NODES)).toBe("some");
    expect(selectionState(new Set(NODES.map((n) => n.id)), NODES)).toBe("all");
    expect(selectionState(new Set(), [])).toBe("none");
  });
});

describe("pills", () => {
  it("ok, slow above 150 ms, failed, or unknown", () => {
    expect(pillTone(true, 42)).toBe("ok");
    expect(pillTone(true, 150)).toBe("ok");
    expect(pillTone(true, 151)).toBe("slow");
    expect(pillTone(true, null)).toBe("ok");
    expect(pillTone(false, 42)).toBe("failed");
    expect(pillTone(null, null)).toBe("unknown");
    expect(pillTone(undefined, undefined)).toBe("unknown");
  });
});

describe("readings", () => {
  it("a probe result replaces the node's row, or joins the list", () => {
    const fresh = { ...NODE_HEALTH[1]!, last_real_ms: 33 };
    expect(mergeHealth(NODE_HEALTH, fresh).map((h) => h.last_real_ms)).toEqual([45, 33, 71, 164, null]);
    expect(mergeHealth(NODE_HEALTH, { ...fresh, node_id: 5 })).toHaveLength(6);
    expect(mergeHealth(undefined, fresh)).toEqual([fresh]);
  });

  it("the card's number: real, else HTTP, else TCP — passing checks only", () => {
    const byId = healthById(ALL_NODE_HEALTH);
    expect(bestReading(byId.get(1))).toEqual({ label: "real", ms: 45 });
    expect(bestReading(byId.get(6))).toEqual({ label: "HTTP", ms: 139 });
    expect(bestReading(byId.get(8))).toEqual({ label: "TCP", ms: 2 });
    expect(bestReading({ ...NODE_HEALTH[0]!, last_real_ok: null, last_http_ok: false, last_tcp_ok: null })).toBeNull();
    expect(bestReading(undefined)).toBeNull();
  });

  it("checked ago on the gateway clock; nothing when never probed", () => {
    expect(checkedAgo(NODE_HEALTH[0]!.checked_at, NOW_SEC * 1000)).toBe("5 s ago");
    expect(checkedAgo(NODE_HEALTH[1]!.checked_at, NOW_SEC * 1000)).toBe("4 min ago");
    expect(checkedAgo(null, NOW_SEC * 1000)).toBeNull();
    expect(checkedAgo("not a date", NOW_SEC * 1000)).toBeNull();
  });

  it("the live probe's word on the active node: fresh latency, failed, or nothing", () => {
    const probe = TRAFFIC_FRAME.active!;
    expect(liveReading(probe)).toBe(42);
    expect(liveReading({ ...probe, stale: true })).toBeNull();
    expect(liveReading({ ...probe, real_ok: false })).toBe("failed");
    expect(liveReading({ ...probe, real_ok: null })).toBeNull();
    expect(liveReading(null)).toBeNull();
  });

  it("the delete confirmation word for word", () => {
    expect(deleteNodeMessage({ name: "vps-hel", address: "198.51.100.23" })).toBe('Delete server "vps-hel" (198.51.100.23)?');
  });
});
