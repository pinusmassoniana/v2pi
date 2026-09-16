import { afterEach, describe, expect, it, vi } from "vitest";
import type { ConnEvent, Routing, Status, TrafficFrame } from "../../api/client";
import { NETWORK, NOW_SEC, ROUTING, STATUS, TRAFFIC_FRAME, node } from "../../test/fixtures";
import {
  FAILOVER_DISMISSED_KEY, bypassState, chartStale, clockTime, eventLevel, failoverBanner, failoverHistory, failoverPill, hasConfigDrift,
  latencyStats, liveLatency, nodeEndpoint, nodeHealthSlot, outboundRates, pathLabel, peakOf, poolSize, rateLines, readFailoverDismissed,
  rollbackStillValid, recentEvents, recentValues, routeBadge, routingSummary, sessionTotals, sinceLabel, tunnelLabel, tunnelLeg, whenLabel,
  writeFailoverDismissed, xrayLabel,
} from "./derive";

const NOW_MS = NOW_SEC * 1000;
const active = TRAFFIC_FRAME.active!;
const status = (patch: Partial<Status>): Status => ({ ...STATUS, ...patch });
const probe = (patch: Partial<NonNullable<TrafficFrame["active"]>>): TrafficFrame["active"] => ({ ...active, ...patch });
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

  it("roll back is valid only while the gateway offers it and it still goes to the confirmed node", () => {
    expect(rollbackStillValid(status({ prev_active_node_id: 2 }), 2)).toBe(true);
    expect(rollbackStillValid(status({ prev_active_node_id: 2, rollback_available: false }), 2)).toBe(false);
    expect(rollbackStillValid(status({ prev_active_node_id: 2, rollback_available: undefined }), 2)).toBe(false);
    expect(rollbackStillValid(status({ prev_active_node_id: 3 }), 2)).toBe(false);
    expect(rollbackStillValid(undefined, 2)).toBe(false);
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

  it("endpoint under the node name", () => {
    expect(nodeEndpoint(node(1, "nl-ams-03"))).toBe("VLESS · Reality · nl-ams-03.example.org:443");
    expect(nodeEndpoint({ ...node(2, "v6"), security: "tls", address: "2001:db8::1", port: 8443 })).toBe("VLESS · TLS · [2001:db8::1]:8443");
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

  it("chart stale: only with an active node whose probe is not fresh, and never on the recorded windows", () => {
    expect(chartStale(600, 1, active)).toBe(false);
    expect(chartStale(600, 1, probe({ stale: true }))).toBe(true);
    expect(chartStale(3_600, 1, null)).toBe(true);            // no probe of the active node yet: not known fresh
    expect(chartStale(600, null, null)).toBe(false);          // no tunnel to judge
    expect(chartStale(600, undefined, probe({ stale: true }))).toBe(false);
    expect(chartStale(86_400, 1, probe({ stale: true }))).toBe(false);
    expect(chartStale(604_800, 1, null)).toBe(false);
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

describe("connection path", () => {
  const withRates = (proxy: [number, number], direct: [number, number]): TrafficFrame => ({
    ...TRAFFIC_FRAME,
    outbounds: { proxy: { down_bps: proxy[0], up_bps: proxy[1] }, direct: { down_bps: direct[0], up_bps: direct[1] } },
  });

  it("outbound rates: the tunnel's and direct traffic's down and up; a missing outbound is 0; no frame is unknown", () => {
    expect(outboundRates(TRAFFIC_FRAME)).toEqual({ proxy: { down: 12_400_000, up: 1_800_000 }, direct: { down: 0, up: 0 } });
    expect(outboundRates({ ...TRAFFIC_FRAME, outbounds: {} })).toEqual({ proxy: { down: 0, up: 0 }, direct: { down: 0, up: 0 } });
    expect(outboundRates(withRates([3_600_000, 400_000], [4_500_000, 300_000]))!.direct).toEqual({ down: 4_500_000, up: 300_000 });
    expect(outboundRates(null)).toBeNull();
  });

  it("rate lines: — without a frame, idle at zero both ways, else one line per direction in its own unit", () => {
    expect(rateLines(null)).toEqual(["—"]);
    expect(rateLines({ down: 0, up: 0 })).toEqual(["idle"]);
    expect(rateLines({ down: 999, up: 0 })).toEqual(["↓ 999 bit/s", "↑ 0 bit/s"]);
    expect(rateLines({ down: 0, up: 1 })).toEqual(["↓ 0 bit/s", "↑ 1 bit/s"]);
    expect(rateLines({ down: 12_400_000, up: 1_800_000 })).toEqual(["↓ 12.4 Mbit/s", "↑ 1.8 Mbit/s"]);
    expect(rateLines({ down: 4_500_000, up: 300_000 })).toEqual(["↓ 4.5 Mbit/s", "↑ 300 kbit/s"]);
  });

  it("node slot: no active node, xray stopped and no frame come first, in that order", () => {
    const none = { leg: "off", note: null, tone: "neutral", ms: null };
    expect(nodeHealthSlot(status({ active_node_id: null, running: false }), null, null)).toEqual({ ...none, state: "none", text: "—" });
    expect(nodeHealthSlot(undefined, TRAFFIC_FRAME, active)).toEqual({ ...none, state: "none", text: "—" });
    expect(nodeHealthSlot(status({ running: false }), null, probe({ real_ok: false }))).toEqual({ ...none, state: "stopped", text: "xray stopped" });
    expect(nodeHealthSlot(STATUS, null, probe({ real_ok: false }))).toEqual({ ...none, state: "no-frame", text: "—" });
  });

  it("node slot: a failed real check, a slow one above 150 ms, a passing one, and OK without a number", () => {
    expect(nodeHealthSlot(STATUS, TRAFFIC_FRAME, probe({ real_ok: false }))).toEqual({ state: "bad", leg: "bad", text: "check failed", note: null, tone: "bad", ms: null });
    expect(nodeHealthSlot(STATUS, TRAFFIC_FRAME, probe({ latency_ms: 151 }))).toEqual({ state: "slow", leg: "ok", text: "151 ms", note: "slow", tone: "warn", ms: 151 });
    expect(nodeHealthSlot(STATUS, TRAFFIC_FRAME, probe({ latency_ms: 150 }))).toEqual({ state: "ok", leg: "ok", text: "150 ms", note: null, tone: "ok", ms: 150 });
    expect(nodeHealthSlot(STATUS, TRAFFIC_FRAME, active)).toMatchObject({ state: "ok", text: "42 ms", tone: "ok", ms: 42 });
    expect(nodeHealthSlot(STATUS, TRAFFIC_FRAME, probe({ latency_ms: null }))).toEqual({ state: "ok", leg: "ok", text: "OK", note: null, tone: "ok", ms: null });
  });

  it("node slot: anything else is stale health — a stale probe, none about the active node, a check with no answer", () => {
    const stale = { state: "stale", leg: "off", text: "health stale", note: null, tone: "neutral", ms: null };
    expect(nodeHealthSlot(STATUS, TRAFFIC_FRAME, probe({ stale: true, real_ok: false }))).toEqual(stale);
    expect(nodeHealthSlot(STATUS, TRAFFIC_FRAME, null)).toEqual(stale);
    expect(nodeHealthSlot(STATUS, TRAFFIC_FRAME, probe({ real_ok: null }))).toEqual(stale);
  });

  it("label: healthy, slow and down read the tunnel, both rates and the kill-switch", () => {
    const name = "nl-ams-03";
    expect(pathLabel({ name, slot: nodeHealthSlot(STATUS, TRAFFIC_FRAME, active), rates: outboundRates(TRAFFIC_FRAME), killSwitch: "ARMED", dim: false })).toBe(
      "Connection path: devices, gateway, nl-ams-03, internet. Tunnel OK, 42 ms, down 12.4 Mbit/s, up 1.8 Mbit/s. Direct by routing rules: idle. Kill-switch ARMED.",
    );
    const slowFrame = withRates([3_600_000, 400_000], [4_500_000, 300_000]);
    expect(pathLabel({ name, slot: nodeHealthSlot(STATUS, slowFrame, probe({ latency_ms: 831 })), rates: outboundRates(slowFrame), killSwitch: "ARMED", dim: false })).toBe(
      "Connection path: devices, gateway, nl-ams-03, internet. Tunnel slow, 831 ms, down 3.6 Mbit/s, up 400 kbit/s. Direct by routing rules: down 4.5 Mbit/s, up 300 kbit/s. Kill-switch ARMED.",
    );
    const idle = withRates([0, 0], [0, 0]);
    expect(pathLabel({ name, slot: nodeHealthSlot(STATUS, idle, probe({ real_ok: false })), rates: outboundRates(idle), killSwitch: "OPEN", dim: false })).toBe(
      "Connection path: devices, gateway, nl-ams-03, internet. Tunnel DOWN, real check failed, idle. Direct by routing rules: idle. Kill-switch OPEN.",
    );
  });

  it("label: an unused leg is OFF, no frame leaves the rates unknown, and a frozen frame says the stats paused", () => {
    expect(pathLabel({ name: "No node", slot: nodeHealthSlot(status({ active_node_id: null }), null, null), rates: null, killSwitch: "UNKNOWN", dim: false })).toBe(
      "Connection path: devices, gateway, No node, internet. Tunnel OFF, rates unknown. Direct by routing rules: rates unknown. Kill-switch UNKNOWN.",
    );
    expect(pathLabel({ name: "nl-ams-03", slot: nodeHealthSlot(STATUS, TRAFFIC_FRAME, active), rates: outboundRates(TRAFFIC_FRAME), killSwitch: "ARMED", dim: true }))
      .toMatch(/Kill-switch ARMED\. Live stats paused\.$/);
  });
});

describe("upstream health", () => {
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

  it("routing: position order, not array order; a value without a type; a direct default names no node", () => {
    const routing: Routing = {
      default_action: "direct", domain_strategy: "AsIs",
      rules: [
        { id: 2, position: 2, type: "", value: "10.0.0.1", action: "BLOCK", enabled: true, label: "" },
        { id: 1, position: 1, type: "port", value: "443", action: "proxy", enabled: true, label: "" },
      ],
    };
    const summary = routingSummary(routing, "nl-ams-03");
    expect(summary.rows.map((r) => [r.badge, r.text])).toEqual([["proxy", "port:443"], ["block", "10.0.0.1"]]);
    expect(summary.header).toBe("2 of 2 rules");
    expect(summary.defaultBadge).toBe("direct");
    expect(summary.defaultTarget).toBeNull();
    expect(routingSummary({ ...routing, default_action: "block" }, "nl-ams-03").defaultTarget).toBeNull();
    expect(routingSummary({ ...routing, default_action: "proxy" }, "No node").defaultTarget).toBe("→ No node");
    expect(routingSummary({ ...routing, rules: [] }, "No node").header).toBe("0 of 0 rules");
    expect(routeBadge("Direct")).toBe("direct");
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

  it("failover history leaves out the kill-switch being armed or disarmed — that switches no node", () => {
    const events: ConnEvent[] = [
      { ts: NOW_SEC - 30, kind: "kill-switch", detail: "disabled" },
      { ts: NOW_SEC - 20, kind: "Kill-Switch", detail: "enabled" },
      { ts: NOW_SEC - 10, kind: "switch", detail: "manual switch" },
    ];
    expect(failoverHistory(events, null).map((e) => e.detail)).toEqual(["manual switch"]);
    expect(failoverHistory(events.slice(0, 2), null)).toEqual([]);
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
