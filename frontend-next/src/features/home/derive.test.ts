import { afterEach, describe, expect, it, vi } from "vitest";
import type { ConnEvent, Network, NodeHealth, Routing, Status, TrafficFrame } from "../../api/client";
import { NETWORK, NODES, NODE_HEALTH, NOW_SEC, ROUTING, STATUS, TRAFFIC_FRAME, node } from "../../test/fixtures";
import {
  FAILOVER_DISMISSED_KEY, activeFlag, activeNode, activeRow, bypassState, clockTime, eventLevel, failoverBanner,
  failoverHistory, failoverPill, hasConfigDrift, ipv6Source, killSwitchState, latencyStats, liveLatency, nodeEndpoint,
  peakOf, poolSize, probeAge, probeFor, readFailoverDismissed, recentEvents, recentValues, routeBadge, routingSummary,
  sessionTotals, sinceLabel, standbyRows, tunnelLabel, tunnelLeg, whenLabel, writeFailoverDismissed, xrayLabel,
} from "./derive";

const NOW_MS = NOW_SEC * 1000;
const active = TRAFFIC_FRAME.active!;
const status = (patch: Partial<Status>): Status => ({ ...STATUS, ...patch });
const probe = (patch: Partial<NonNullable<TrafficFrame["active"]>>): TrafficFrame["active"] => ({ ...active, ...patch });
const withStatus = (patch: Partial<Network["status"]>): Network => ({ ...NETWORK, status: { ...NETWORK.status, ...patch } });
const pad = (n: number) => String(n).padStart(2, "0");

afterEach(() => localStorage.clear());

describe("status block", () => {
  it("xray: running wins, then a supervisor error reconnects, then stopped, then nothing known", () => {
    expect(xrayLabel(status({ running: true, xray_state: "error" }))).toEqual({ label: "RUNNING", tone: "ok" });
    expect(xrayLabel(status({ running: false, xray_state: "error" }))).toEqual({ label: "RECONNECTING", tone: "warn" });
    expect(xrayLabel(status({ running: false, xray_state: "stopped" }))).toEqual({ label: "STOPPED", tone: "bad" });
    expect(xrayLabel(undefined)).toEqual({ label: "—", tone: "neutral" });
  });

  it("config drift only when proven: unknown and missing are not problems", () => {
    expect(hasConfigDrift(status({ config_drift: "drift" }))).toBe(true);
    expect(hasConfigDrift(status({ config_drift: "unknown" }))).toBe(false);
    expect(hasConfigDrift(status({ config_drift: "ok" }))).toBe(false);
    expect(hasConfigDrift(status({ config_drift: undefined }))).toBe(false);
    expect(hasConfigDrift(undefined)).toBe(false);
  });

  it("tunnel: unknown while the status poll fails or before it answers, whatever it said last", () => {
    expect(tunnelLabel(STATUS, true, active)).toEqual({ label: "UNKNOWN", tone: "neutral" });
    expect(tunnelLabel(status({ active_node_id: null }), true, null).label).toBe("UNKNOWN");
    expect(tunnelLabel(undefined, false, active)).toEqual({ label: "UNKNOWN", tone: "neutral" });
  });

  it("tunnel: offline with no active node, with xray stopped, or on a fresh failed probe of the active node", () => {
    expect(tunnelLabel(status({ active_node_id: null, tunnel_online: false }), false, null)).toEqual({ label: "OFFLINE", tone: "bad" });
    expect(tunnelLabel(status({ running: false }), false, active)).toEqual({ label: "OFFLINE", tone: "bad" });
    expect(tunnelLabel(STATUS, false, probe({ real_ok: false }))).toEqual({ label: "OFFLINE", tone: "bad" });
    // a failed probe that is stale, or about another node, proves nothing
    expect(tunnelLabel(STATUS, false, probe({ real_ok: false, stale: true })).label).toBe("UNKNOWN");
    expect(tunnelLabel(STATUS, false, probe({ real_ok: false, node_id: 2 })).label).toBe("ONLINE");
  });

  it("tunnel: unknown while health is not fresh; online when the gateway says so; unknown otherwise", () => {
    expect(tunnelLabel(STATUS, false, probe({ stale: true }))).toEqual({ label: "UNKNOWN", tone: "neutral" });
    expect(tunnelLabel(status({ active_health_fresh: false }), false, null).label).toBe("UNKNOWN");
    expect(tunnelLabel(STATUS, false, active)).toEqual({ label: "ONLINE", tone: "ok" });
    expect(tunnelLabel(STATUS, false, null)).toEqual({ label: "ONLINE", tone: "ok" });
    expect(tunnelLabel(status({ tunnel_online: false }), false, active)).toEqual({ label: "UNKNOWN", tone: "neutral" });
    expect(tunnelLabel(status({ tunnel_online: null }), false, null).label).toBe("UNKNOWN");
    expect(tunnelLabel(status({ tunnel_online: undefined }), false, null).label).toBe("UNKNOWN");
  });

  it("kill-switch: disabled is open, armed only on confirmed enforcement", () => {
    expect(killSwitchState(NETWORK)).toEqual({ label: "ARMED", tone: "ok" });
    expect(killSwitchState({ ...NETWORK, kill_switch_enabled: false })).toEqual({ label: "OPEN", tone: "bad" });
    expect(killSwitchState(withStatus({ enforcement_status: "error" })).label).toBe("UNKNOWN");
    expect(killSwitchState(withStatus({ enforcement_status: undefined })).label).toBe("UNKNOWN");
    expect(killSwitchState(undefined).label).toBe("UNKNOWN");
  });

  it("pool size on one /24, including unusual but valid ranges", () => {
    expect(poolSize(NETWORK.segment)).toBe(50);
    expect(poolSize({ dhcp_start: "10.0.2.7", dhcp_end: "10.0.2.7" })).toBe(1);
    expect(poolSize({ dhcp_start: "192.168.1.0", dhcp_end: "192.168.1.255" })).toBe(256);
    expect(poolSize({ dhcp_start: " 10.0.2.10", dhcp_end: "10.0.2.20 " })).toBe(11);
  });

  it("pool size is unknown for reversed, cross-/24, malformed or missing ranges", () => {
    expect(poolSize({ dhcp_start: "10.0.2.149", dhcp_end: "10.0.2.100" })).toBeNull();
    expect(poolSize({ dhcp_start: "10.0.2.200", dhcp_end: "10.0.3.20" })).toBeNull();
    expect(poolSize({ dhcp_start: "10.0.2", dhcp_end: "10.0.2.9" })).toBeNull();
    expect(poolSize({ dhcp_start: "10.0.2.300", dhcp_end: "10.0.2.310" })).toBeNull();
    expect(poolSize({ dhcp_start: "fd00::10", dhcp_end: "fd00::20" })).toBeNull();
    expect(poolSize({ dhcp_start: "", dhcp_end: "" })).toBeNull();
    expect(poolSize(undefined)).toBeNull();
  });

  it("active node, endpoint and flag; none without an active node", () => {
    expect(activeNode(NODES, 2)?.name).toBe("de-fra-01");
    expect(activeNode(NODES, null)).toBeUndefined();
    expect(activeNode(undefined, 1)).toBeUndefined();
    expect(nodeEndpoint(node(1, "nl-ams-03"))).toBe("VLESS · Reality · nl-ams-03.example.org:443");
    expect(nodeEndpoint({ ...node(2, "v6"), security: "tls", address: "2001:db8::1", port: 8443 })).toBe("VLESS · TLS · [2001:db8::1]:8443");
    expect(activeFlag(probeFor(TRAFFIC_FRAME, 1))).toBe("🇳🇱");
    expect(activeFlag(probeFor(TRAFFIC_FRAME, 2))).toBe("");
    expect(activeFlag(null)).toBe("");
  });

  it("probeFor: the live probe only when it is about the active node", () => {
    expect(probeFor(TRAFFIC_FRAME, 1)).toBe(active);
    expect(probeFor(TRAFFIC_FRAME, 2)).toBeNull();          // the frame still describes the previous node
    expect(probeFor(TRAFFIC_FRAME, null)).toBeNull();
    expect(probeFor(TRAFFIC_FRAME, undefined)).toBeNull();
    expect(probeFor({ ...TRAFFIC_FRAME, active: null }, 1)).toBeNull();
    expect(probeFor(null, 1)).toBeNull();
  });
});

describe("live traffic", () => {
  it("session totals: session, else lifetime, else since xray start", () => {
    expect(sessionTotals(TRAFFIC_FRAME)).toEqual({ up: 1_240_000_000, down: 18_600_000_000, source: "session" });
    const noSession: TrafficFrame = { ...TRAFFIC_FRAME, session: undefined };
    expect(sessionTotals(noSession)).toEqual({ up: 9_000_000_000, down: 120_000_000_000, source: "lifetime" });
    expect(sessionTotals({ ...noSession, lifetime: undefined })).toEqual({ up: 1_300_000_000, down: 19_000_000_000, source: "totals" });
    expect(sessionTotals(null)).toBeNull();
  });

  it("bypass: any direct traffic leaks, the alert waits for more than 50 000 bps", () => {
    const frame = (up: number, down: number): TrafficFrame => ({ ...TRAFFIC_FRAME, outbounds: { ...TRAFFIC_FRAME.outbounds, direct: { up_bps: up, down_bps: down } } });
    expect(bypassState(TRAFFIC_FRAME)).toEqual({ down: 0, up: 0, total: 0, alert: false, leaking: false });
    expect(bypassState(frame(10_000, 40_000))).toMatchObject({ total: 50_000, alert: false, leaking: true });
    expect(bypassState(frame(10_001, 40_000))).toMatchObject({ total: 50_001, alert: true, leaking: true });
    expect(bypassState({ ...TRAFFIC_FRAME, outbounds: {} })).toMatchObject({ total: 0, leaking: false });
    expect(bypassState(null).alert).toBe(false);
  });

  it("recent values: the trailing window of one direction, thinned to the point budget", () => {
    const samples = Array.from({ length: 600 }, (_, i) => ({ ts: i * 1000, up: i, down: i * 2 }));
    const down = recentValues(samples, 300_000, "down");
    expect(down.length).toBeLessThanOrEqual(60);
    expect(down[0]).toBe(300 * 2);           // the first sample strictly inside the last 300 s
    expect(recentValues(samples, 3_000, "up")).toEqual([597, 598, 599]);
    expect(recentValues([], 300_000, "down")).toEqual([]);
  });

  it("peak: the highest download, the first one on a tie, nothing when idle", () => {
    expect(peakOf([{ ts: 1, up: 9, down: 5 }, { ts: 2, up: 0, down: 8 }, { ts: 3, up: 0, down: 8 }])).toEqual({ bps: 8, ts: 2 });
    expect(peakOf([{ ts: 1, up: 100, down: 0 }])).toBeNull();
    expect(peakOf([])).toBeNull();
  });

  it("tunnel leg: off when xray is down or health is stale, bad on a failed check, ok on a passing one", () => {
    expect(tunnelLeg(STATUS, active)).toBe("ok");
    expect(tunnelLeg(STATUS, probe({ real_ok: false }))).toBe("bad");
    expect(tunnelLeg(STATUS, probe({ real_ok: null }))).toBe("off");
    expect(tunnelLeg(STATUS, probe({ stale: true }))).toBe("off");
    expect(tunnelLeg(status({ running: false }), active)).toBe("off");
    expect(tunnelLeg(STATUS, null)).toBe("off");
  });

  it("latency stats: fresh with the history's average, else stale or unknown with the probe's age", () => {
    expect(liveLatency(active)).toBe(42);
    expect(latencyStats(active, NOW_MS)).toEqual({ ms: 42, avg: 49, fresh: true, sub: "avg 49 ms · live" });
    expect(latencyStats(probe({ lat_history: [] }), NOW_MS)).toMatchObject({ ms: 42, avg: null, sub: "live" });
    expect(latencyStats(probe({ stale: true, checked_at: new Date(NOW_MS - 180_000).toISOString() }), NOW_MS))
      .toMatchObject({ ms: null, fresh: false, sub: "stale · 3m ago" });
    expect(latencyStats(probe({ real_ok: false }), NOW_MS).sub).toBe("unknown · 5s ago");
    expect(latencyStats(probe({ stale: true, checked_at: null }), NOW_MS).sub).toBe("stale");
    expect(latencyStats(null, NOW_MS)).toEqual({ ms: null, avg: null, fresh: false, sub: "unknown" });
  });
});

describe("upstream health", () => {
  it("probe age in the largest whole unit", () => {
    expect(probeAge(12_400)).toBe("12 s");
    expect(probeAge(240_000)).toBe("4 min");
    expect(probeAge(3 * 3_600_000)).toBe("3 h");
    expect(probeAge(50 * 3_600_000)).toBe("2 d");
    expect(probeAge(-1)).toBe("0 s");
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

  it("failover pill: ready or not while online, unknown while the tunnel is, otherwise offline", () => {
    const online = { label: "ONLINE", tone: "ok" } as const;
    expect(failoverPill(STATUS, online)).toEqual({ label: "FAILOVER READY", tone: "ok" });
    expect(failoverPill(status({ failover_ready: false }), online)).toEqual({ label: "NO ELIGIBLE STANDBY", tone: "warn" });
    expect(failoverPill(status({ failover_ready: undefined }), online).label).toBe("NO ELIGIBLE STANDBY");
    expect(failoverPill(STATUS, { label: "UNKNOWN", tone: "neutral" })).toEqual({ label: "UNKNOWN", tone: "neutral" });
    expect(failoverPill(STATUS, { label: "OFFLINE", tone: "bad" })).toEqual({ label: "OFFLINE", tone: "bad" });
  });
});

describe("failover alert", () => {
  const at = NOW_SEC - 600;

  it("shows a failover from the last 24 h until exactly that one is dismissed", () => {
    expect(failoverBanner(at, null, NOW_MS)).toBe(at);
    expect(failoverBanner(at, at, NOW_MS)).toBeNull();
    expect(failoverBanner(NOW_SEC - 60, at, NOW_MS)).toBe(NOW_SEC - 60);   // a newer failover shows again
    expect(failoverBanner(NOW_SEC - 86_400, null, NOW_MS)).toBeNull();
    expect(failoverBanner(NOW_SEC - 86_399, null, NOW_MS)).toBe(NOW_SEC - 86_399);
    expect(failoverBanner(null, null, NOW_MS)).toBeNull();
    expect(failoverBanner(undefined, null, NOW_MS)).toBeNull();
  });

  it("remembers the dismissal under the Svelte panel's key", () => {
    expect(readFailoverDismissed()).toBeNull();
    writeFailoverDismissed(at);
    expect(localStorage.getItem(FAILOVER_DISMISSED_KEY)).toBe(String(at));
    expect(readFailoverDismissed()).toBe(at);
    localStorage.setItem(FAILOVER_DISMISSED_KEY, "garbage");
    expect(readFailoverDismissed()).toBeNull();
  });

  it("a blocked dismissal key reads as none and never throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new DOMException("blocked", "SecurityError"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("blocked", "SecurityError"); });
    expect(readFailoverDismissed()).toBeNull();
    expect(() => writeFailoverDismissed(at)).not.toThrow();
  });
});

describe("summaries", () => {
  it("routing: first four enabled rules by position, the total, and the default towards the active node", () => {
    const summary = routingSummary(ROUTING, "nl-ams-03");
    expect(summary.rows.map((r) => [r.badge, r.text])).toEqual([
      ["proxy", "domain:netflix.com"], ["direct", "geoip:ru"], ["block", "geosite:category-ads-all"], ["direct", "domain:gosuslugi.ru"],
    ]);
    expect(summary.header).toBe("4 of 6 rules");
    expect(summary.defaultBadge).toBe("proxy");
    expect(summary.defaultTarget).toBe("→ nl-ams-03");
  });

  it("routing: position order, not array order; a value without a type; no active node", () => {
    const routing: Routing = {
      default_action: "direct", domain_strategy: "AsIs",
      rules: [
        { id: 2, position: 2, type: "", value: "10.0.0.1", action: "BLOCK", enabled: true, label: "" },
        { id: 1, position: 1, type: "port", value: "443", action: "proxy", enabled: true, label: "" },
      ],
    };
    const summary = routingSummary(routing, null);
    expect(summary.rows.map((r) => [r.badge, r.text])).toEqual([["proxy", "port:443"], ["block", "10.0.0.1"]]);
    expect(summary.header).toBe("2 of 2 rules");
    expect(summary.defaultTarget).toBe("→ —");
    expect(routingSummary({ ...routing, rules: [] }, null).header).toBe("0 of 0 rules");
    expect(routeBadge("Direct")).toBe("direct");
  });

  it("IPv6 source", () => {
    expect(ipv6Source(NETWORK)).toBe("static");
    expect(ipv6Source(withStatus({ ipv6_prefix_source: "pd" }))).toBe("pd");
    expect(ipv6Source(withStatus({ ipv6_prefix_source: null }))).toBe("on");
    expect(ipv6Source({ ...NETWORK, ipv6_enabled: false })).toBe("off");
  });
});

describe("events", () => {
  it("level: bad before warn before ok, info otherwise", () => {
    expect(eventLevel("failover")).toBe("bad");
    expect(eventLevel("LEAK")).toBe("bad");
    expect(eventLevel("uplink down")).toBe("bad");
    expect(eventLevel("retry")).toBe("warn");
    expect(eventLevel("health stale")).toBe("warn");
    expect(eventLevel("apply")).toBe("ok");
    expect(eventLevel("recovered")).toBe("ok");
    expect(eventLevel("subscription")).toBe("info");
    expect(eventLevel("")).toBe("info");
  });

  it("recent: the last six, newest first, whatever order they arrive in", () => {
    const recent = recentEvents(NETWORK.events);
    expect(recent).toHaveLength(6);
    expect(recent[0]!.kind).toBe("leak");
    expect(recent.map((e) => e.detail)).not.toContain("config applied for fi-hel-02");
    const shuffled: ConnEvent[] = [NETWORK.events[3]!, NETWORK.events[6]!, NETWORK.events[0]!];
    expect(recentEvents(shuffled).map((e) => e.kind)).toEqual(["leak", "retry", "apply"]);
  });

  it("failover history: failover and switch kinds, newest first, at most eight", () => {
    const events: ConnEvent[] = Array.from({ length: 10 }, (_, i) => ({ ts: NOW_SEC - 1000 + i, kind: i % 2 ? "Failover" : "switch", detail: `#${i}` }));
    const history = failoverHistory([...events, { ts: NOW_SEC, kind: "apply", detail: "not a switch" }], null);
    expect(history.map((e) => e.detail)).toEqual(["#9", "#8", "#7", "#6", "#5", "#4", "#3", "#2"]);
    expect(failoverHistory(NETWORK.events, null).map((e) => e.ts)).toEqual([NOW_SEC - 600, NOW_SEC - 50_000]);
  });

  it("failover history falls back to last_failover_at when no event matches, and is empty without either", () => {
    const plain: ConnEvent[] = [{ ts: NOW_SEC - 5, kind: "apply", detail: "applied" }];
    expect(failoverHistory(plain, NOW_SEC - 700)).toEqual([{ ts: NOW_SEC - 700, kind: "failover", detail: "Auto-failover — the gateway switched node" }]);
    expect(failoverHistory(plain, null)).toEqual([]);
    expect(failoverHistory([], undefined)).toEqual([]);
  });

  it("time labels in local time", () => {
    const at = new Date(NOW_MS - 30_000);
    const clock = `${pad(at.getHours())}:${pad(at.getMinutes())}:${pad(at.getSeconds())}`;
    expect(clockTime(NOW_SEC - 30)).toBe(clock);
    expect(whenLabel(NOW_SEC - 30, NOW_MS + 0)).toMatch(new RegExp(`^(${clock}|\\d{2} [A-Z][a-z]{2} · ${clock})$`));
    const lastWeek = new Date(NOW_MS - 7 * 86_400_000);
    expect(whenLabel(lastWeek.getTime() / 1000, NOW_MS)).toMatch(/^\d{2} [A-Z][a-z]{2} · \d{2}:\d{2}:\d{2}$/);
    expect(sinceLabel(lastWeek.getTime() / 1000)).toMatch(new RegExp(`^${pad(lastWeek.getDate())} [A-Z][a-z]{2} ${pad(lastWeek.getHours())}:${pad(lastWeek.getMinutes())}$`));
  });
});
