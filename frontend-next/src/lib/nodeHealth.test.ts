import { describe, expect, it } from "vitest";
import type { NodeHealth, Status, TrafficFrame } from "../api/client";
import { NODES, NODE_HEALTH, NOW_SEC, STATUS, TRAFFIC_FRAME } from "../test/fixtures";
import {
  SLOW_LATENCY_MS, activeFlag, activeNode, activeNodeLabel, activeRow, checkedAgo, everProbed, nodeLabel, probeAge, probeFor, standbyRows,
  timestampMs,
} from "./nodeHealth";

const NOW_MS = NOW_SEC * 1000;
const active = TRAFFIC_FRAME.active!;
const status = (patch: Partial<Status>): Status => ({ ...STATUS, ...patch });
const probe = (patch: Partial<NonNullable<TrafficFrame["active"]>>): TrafficFrame["active"] => ({ ...active, ...patch });

describe("active node", () => {
  it("finds the active node; none without an active node or a list", () => {
    expect(activeNode(NODES, 2)?.name).toBe("de-fra-01");
    expect(activeNode(NODES, null)).toBeUndefined();
    expect(activeNode(undefined, 1)).toBeUndefined();
  });

  it("node labels: the name, node #N before the list loads or once it no longer lists the node, No node when none is active", () => {
    expect(activeNodeLabel(STATUS, NODES)).toBe("nl-ams-03");
    expect(activeNodeLabel(STATUS, undefined)).toBe("node #1");
    expect(activeNodeLabel(status({ active_node_id: 9 }), NODES)).toBe("node #9");
    expect(activeNodeLabel(status({ active_node_id: null }), NODES)).toBe("No node");
    expect(activeNodeLabel(undefined, NODES)).toBe("No node");
    expect(nodeLabel(NODES, 2)).toBe("de-fra-01");
    expect(nodeLabel([], 2)).toBe("node #2");
  });

  it("probeFor: the live probe only when it is about the active node", () => {
    expect(probeFor(TRAFFIC_FRAME, 1)).toBe(active);
    expect(probeFor(TRAFFIC_FRAME, 2)).toBeNull();          // the frame still describes the previous node
    expect(probeFor(TRAFFIC_FRAME, null)).toBeNull();
    expect(probeFor(TRAFFIC_FRAME, undefined)).toBeNull();
    expect(probeFor({ ...TRAFFIC_FRAME, active: null }, 1)).toBeNull();
    expect(probeFor(null, 1)).toBeNull();
  });

  it("flag of the active node's egress, from its matched probe only", () => {
    expect(activeFlag(probeFor(TRAFFIC_FRAME, 1))).toBe("🇳🇱");
    expect(activeFlag(probeFor(TRAFFIC_FRAME, 2))).toBe("");
    expect(activeFlag(null)).toBe("");
  });
});

describe("probe results", () => {
  it("slow is anything above 150 ms", () => {
    expect(SLOW_LATENCY_MS).toBe(150);
  });

  it("probe age in the largest whole unit", () => {
    expect(probeAge(12_400)).toBe("12 s");
    expect(probeAge(240_000)).toBe("4 min");
    expect(probeAge(3 * 3_600_000)).toBe("3 h");
    expect(probeAge(50 * 3_600_000)).toBe("2 d");
    expect(probeAge(-1)).toBe("0 s");
  });

  it("never probed is a missing or unreadable checked_at; checked ago runs on the gateway clock", () => {
    expect(timestampMs(NODE_HEALTH[0]!.checked_at)).toBe(NOW_MS - 5_000);
    expect(everProbed(NODE_HEALTH[0]!.checked_at)).toBe(true);
    for (const at of [null, undefined, "", "not a date"]) {
      expect(timestampMs(at)).toBeNull();
      expect(everProbed(at)).toBe(false);
      expect(checkedAgo(at, NOW_MS)).toBeNull();
    }
    expect(checkedAgo(NODE_HEALTH[0]!.checked_at, NOW_MS)).toBe("5 s ago");
    expect(checkedAgo(NODE_HEALTH[1]!.checked_at, NOW_MS)).toBe("4 min ago");
  });

  it("active row: live, probe failed, or health stale; flag only from a probe of this node", () => {
    const n1 = NODES[0]!;
    expect(activeRow(n1, active)).toEqual({ id: 1, name: "nl-ams-03", flag: "🇳🇱", ms: 42, state: "live", age: null, dim: false, active: true });
    expect(activeRow(n1, probe({ real_ok: false }))).toMatchObject({ state: "failed", ms: null });
    expect(activeRow(n1, probe({ stale: true }))).toMatchObject({ state: "stale", ms: null });
    expect(activeRow(n1, probe({ real_ok: null }))).toMatchObject({ state: "stale" });
    expect(activeRow(n1, null)).toMatchObject({ state: "stale", flag: "" });
    expect(activeRow(NODES[1], active)).toMatchObject({ state: "stale", flag: "" });   // the frame is about another node
    expect(activeRow(undefined, active)).toBeNull();
  });

  it("standby: up to four, lowest latency first, then failed, then never probed; dimmed past ten minutes", () => {
    const rows = standbyRows(NODES, NODE_HEALTH, 1, NOW_MS);
    expect(rows.map((r) => [r.name, r.state, r.ms, r.age, r.dim, r.flag])).toEqual([
      ["de-fra-01", "measured", 58, "4 min", false, "🇩🇪"],
      ["fi-hel-02", "measured", 71, "6 min", false, "🇫🇮"],
      ["pl-waw-01", "measured", 164, "23 min", true, "🇵🇱"],
      ["se-sto-01", "failed", null, "12 min", true, "🇸🇪"],
    ]);
    const all = standbyRows(NODES, NODE_HEALTH, 1, NOW_MS, Infinity);
    expect(all.at(-1)).toEqual({ id: 5, name: "ch-zrh-02", flag: "", ms: null, state: "not-probed", age: null, dim: false, active: false });
  });

  it("standby: TCP latency stands in for a missing real check; no numbers at all is not probed", () => {
    const health: NodeHealth[] = [
      { ...NODE_HEALTH[1]!, last_real_ms: null, last_tcp_ms: 33 },
      { ...NODE_HEALTH[2]!, last_real_ms: null, last_tcp_ms: null },
      { ...NODE_HEALTH[3]!, checked_at: "not a date" },
    ];
    const rows = standbyRows(NODES.slice(0, 4), health, 1, NOW_MS);
    expect(rows.map((r) => [r.name, r.state, r.ms])).toEqual([
      ["de-fra-01", "measured", 33], ["fi-hel-02", "not-probed", null], ["pl-waw-01", "not-probed", null],
    ]);
  });

  it("standby with no active node lists every node; with no health every node is not probed", () => {
    expect(standbyRows(NODES, NODE_HEALTH, null, NOW_MS, Infinity)).toHaveLength(6);
    expect(standbyRows(NODES, [], 1, NOW_MS).every((r) => r.state === "not-probed")).toBe(true);
    expect(standbyRows([], NODE_HEALTH, 1, NOW_MS)).toEqual([]);
  });
});
