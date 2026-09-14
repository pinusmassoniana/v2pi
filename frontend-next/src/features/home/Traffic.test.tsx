import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError, type Status, type TrafficFrame, type TrafficHistoryResp } from "../../api/client";
import { createQueryClient } from "../../api/queryClient";
import { latencyGeometry } from "../../components/data/latencyGeometry";
import { NETWORK, NOW_SEC, STATUS, TRAFFIC_FRAME, mockApi } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";
import { failoverHistory } from "./derive";

// Pass-through spies: failoverHistory runs only when the page itself renders, latencyGeometry only when the
// latency chart rebuilds its paths — so their call counts show what a live frame re-renders.
vi.mock("./derive", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./derive")>();
  return { ...actual, failoverHistory: vi.fn(actual.failoverHistory) };
});
vi.mock("../../components/data/latencyGeometry", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../components/data/latencyGeometry")>();
  return { ...actual, latencyGeometry: vi.fn(actual.latencyGeometry) };
});

const pad = (n: number) => String(n).padStart(2, "0");
const clock = (ms: number) => {
  const d = new Date(ms);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
};
const region = (name: string | RegExp) => screen.getByRole("region", { name });
const probe = (patch: Partial<NonNullable<TrafficFrame["active"]>>, ts = TRAFFIC_FRAME.ts): TrafficFrame => ({
  ...TRAFFIC_FRAME, ts, active: { ...TRAFFIC_FRAME.active!, ...patch },
});

async function openTraffic(status: Partial<Status> = {}, client = createQueryClient()) {
  const api$ = mockApi();
  api$.getStatus.mockResolvedValue({ ...STATUS, ...status });
  const view = renderApp("/traffic", { client });
  await screen.findByRole("region", { name: "Failover history" });
  await within(await screen.findByRole("region", { name: "Probe latency by node" })).findByRole("list", { name: "Nodes by latency" });
  return { api$, ...view };
}

describe("Traffic › KPIs", () => {
  it("H1: peak download of the window with its time, live latency with its average, failovers with the last switch, coarse uptime", async () => {
    const { api$ } = await openTraffic();
    api$.emitTraffic({ ...TRAFFIC_FRAME, ts: TRAFFIC_FRAME.ts - 3_000, outbounds: { proxy: { up_bps: 5, down_bps: 18_200_000 } } });
    api$.emitTraffic(TRAFFIC_FRAME);
    expect(region("Peak download · 10m")).toHaveTextContent(`18.2Mbit/sat ${clock(TRAFFIC_FRAME.ts - 3_000)}`);
    expect(region("Active latency")).toHaveTextContent("42msavg 49 ms · live");
    const failovers = region("Failovers · 24h");
    expect(failovers).toHaveTextContent(`2last ${clock((NOW_SEC - 600) * 1000)}`);
    expect(within(failovers).getByText("2")).toHaveClass("text-warn");
    expect(region("Uptime")).toHaveTextContent(/^Uptime1msince \d{2} [A-Z][a-z]{2} \d{2}:\d{2}$/);
  });

  it("H1: the peak follows the selected window", async () => {
    const { api$ } = await openTraffic();
    api$.emitTraffic(TRAFFIC_FRAME);
    await userEvent.click(within(region("Throughput")).getByRole("button", { name: "1h" }));
    expect(region("Peak download · 1h")).toHaveTextContent("12.4Mbit/s");
  });

  it("H1: stale or unknown latency shows no number, just the probe's age", async () => {
    const { api$ } = await openTraffic();
    expect(region("Active latency")).toHaveTextContent("—unknown");
    api$.emitTraffic(probe({ stale: true, checked_at: new Date((NOW_SEC - 180) * 1000).toISOString() }));
    expect(region("Active latency")).toHaveTextContent(/^Active latency—stale · 3m ago$/);
  });

  it("H1: failovers fall back to the status count, and there is no uptime while offline", async () => {
    const api$ = mockApi();
    api$.getStatus.mockResolvedValue({ ...STATUS, active_node_id: null, tunnel_online: false, failovers_24h: 0 });
    api$.getNetwork.mockResolvedValue({ ...NETWORK, events: [], status: { ...NETWORK.status, failovers_24h: undefined } });
    renderApp("/traffic");
    const failovers = await screen.findByRole("region", { name: "Failovers · 24h" });
    await waitFor(() => expect(failovers).toHaveTextContent("0none recorded"));
    expect(region("Uptime")).toHaveTextContent("Uptime—not connected");
  });

  it("H1: uptime reads unknown while health is not fresh, as the topbar does", async () => {
    await openTraffic({ tunnel_online: false, active_health_fresh: false });
    await waitFor(() => expect(region("Uptime")).toHaveTextContent("Uptime—unknown"));
    expect(screen.getByText("Tunnel unknown")).toBeInTheDocument();
  });

  it("H1: a fresh failed probe of the active node stops the uptime, as the topbar does", async () => {
    const { api$ } = await openTraffic();
    api$.emitTraffic(TRAFFIC_FRAME);
    expect(region("Uptime")).toHaveTextContent(/^Uptime1msince/);
    api$.emitTraffic(probe({ real_ok: false }));
    expect(region("Uptime")).toHaveTextContent("Uptime—not connected");
    expect(screen.getByText("Tunnel offline")).toBeInTheDocument();
  });
});

describe("Traffic › live frames", () => {
  it("a live frame re-renders the cards that show it, not the page", async () => {
    const { api$ } = await openTraffic();
    api$.emitTraffic({ ...TRAFFIC_FRAME, ts: TRAFFIC_FRAME.ts - 1_000 });
    const pageRenders = vi.mocked(failoverHistory).mock.calls.length;
    for (let i = 0; i < 5; i++) api$.emitTraffic({ ...TRAFFIC_FRAME, ts: TRAFFIC_FRAME.ts + i * 1_000 });
    expect(region("Active latency")).toHaveTextContent("42ms");
    expect(vi.mocked(failoverHistory).mock.calls.length).toBe(pageRenders);
  });

  it("the latency chart keeps its geometry while frames repeat the same probe history", async () => {
    const { api$ } = await openTraffic();
    api$.emitTraffic(TRAFFIC_FRAME);
    const builds = vi.mocked(latencyGeometry).mock.calls.length;
    for (let i = 1; i <= 5; i++) {
      api$.emitTraffic({ ...TRAFFIC_FRAME, ts: TRAFFIC_FRAME.ts + i * 1_000, active: { ...TRAFFIC_FRAME.active!, lat_history: [...TRAFFIC_FRAME.active!.lat_history] } });
    }
    expect(vi.mocked(latencyGeometry).mock.calls.length).toBe(builds);
    api$.emitTraffic(probe({ lat_history: [...TRAFFIC_FRAME.active!.lat_history, 60] }, TRAFFIC_FRAME.ts + 6_000));
    expect(vi.mocked(latencyGeometry).mock.calls.length).toBe(builds + 1);
    expect(region("Active node latency")).toHaveTextContent("last 10 probes");
  });

  it("while the recorded 24 h window loads, the peak reads — rather than no traffic", async () => {
    const { api$ } = await openTraffic();
    api$.getTrafficHistory.mockReturnValue(new Promise<TrafficHistoryResp>(() => {}));
    await userEvent.click(within(region("Throughput")).getByRole("button", { name: "24h" }));
    expect(region("Peak download · 24h")).toHaveTextContent(/^Peak download · 24h——$/);
  });
});

describe("Traffic › charts", () => {
  it("H2: the throughput chart with the same windows; 7d reads recorded history", async () => {
    const { api$ } = await openTraffic();
    const card = region("Throughput");
    expect(within(within(card).getByRole("group", { name: "Chart window" })).getAllByRole("button").map((b) => b.textContent)).toEqual(["1m", "10m", "1h", "24h", "7d"]);
    await userEvent.click(within(card).getByRole("button", { name: "7d" }));
    await waitFor(() => expect(api$.getTrafficHistory.mock.calls.some(([sec]) => sec === 604_800)).toBe(true));
    expect(region("Peak download · 7d")).toBeInTheDocument();
  });

  it("H3: every node on one 0–250 ms scale — active live, standbys by latency, failed, never probed", async () => {
    const { api$ } = await openTraffic();
    api$.emitTraffic(TRAFFIC_FRAME);
    const list = within(region("Probe latency by node")).getByRole("list", { name: "Nodes by latency" });
    expect(within(list).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "🇳🇱 nl-ams-0342 mslive",
      "🇩🇪 de-fra-0158 ms· 4 min",
      "🇫🇮 fi-hel-0271 ms· 6 min",
      "🇵🇱 pl-waw-01164 ms· 23 min",
      "🇸🇪 se-sto-01failed· 12 min",
      "ch-zrh-02not probed",
    ]);
    expect(within(list).getAllByRole("listitem")[0]!.querySelector("i")).toHaveClass("bg-brand");
    expect(within(list).getAllByRole("listitem")[3]!.querySelector("i")).toHaveClass("bg-warn");
    expect(region("Probe latency by node")).toHaveTextContent("slow > 150 ms");
  });

  it("H4: the active node's last probes with average and degraded marks", async () => {
    const { api$ } = await openTraffic();
    api$.emitTraffic(TRAFFIC_FRAME);
    const card = region("Active node latency");
    expect(card).toHaveTextContent("🇳🇱 nl-ams-03 · last 9 probes");
    expect(within(card).getByRole("img")).toHaveAccessibleName("Latency of the last 9 probes, average 49 ms");
    expect(card.querySelectorAll('[data-marker="degraded"]')).toHaveLength(1);
  });

  it("H4: a failed current probe marks the last point and dims the chart; no history says so", async () => {
    const { api$ } = await openTraffic();
    expect(within(region("Active node latency")).getByText("no probe history yet")).toBeInTheDocument();
    api$.emitTraffic(probe({ real_ok: false }));
    const card = region("Active node latency");
    expect(card.querySelector('[data-marker="failed"]')).not.toBeNull();
    expect(card.querySelector("[data-dim]")).not.toBeNull();
  });

  it("a live frame about another node shows the active node's latency as unknown", async () => {
    const { api$ } = await openTraffic();
    api$.emitTraffic(probe({ node_id: 2 }));
    expect(region("Active latency")).toHaveTextContent(/^Active latency—unknown$/);
    const card = region("Active node latency");
    expect(card).toHaveTextContent("nl-ams-03");
    expect(card).not.toHaveTextContent("🇳🇱");
    expect(within(card).getByText("no probe history yet")).toBeInTheDocument();
    const bars = within(region("Probe latency by node")).getByRole("list", { name: "Nodes by latency" });
    expect(within(bars).getAllByRole("listitem")[0]).toHaveTextContent("nl-ams-03health stale");
    expect(within(region("Throughput")).getByRole("status")).toHaveTextContent("Tunnel health is stale");
  });

  it("stats off: no peak, and the chart points to System › Panel", async () => {
    const { api$ } = await openTraffic();
    api$.emitTraffic(TRAFFIC_FRAME);
    api$.emitTraffic({ disabled: true });
    expect(region(/^Peak download/)).toHaveTextContent("—no traffic in this window");
    expect(within(region("Throughput")).getByText("Traffic stats are off")).toBeInTheDocument();
  });
});

describe("Traffic › failover history", () => {
  it("H5: failover and switch events, newest first, as a table on desktop and rows on a phone", async () => {
    await openTraffic();
    const card = region("Failover history");
    const table = within(card).getByRole("table");
    expect(within(table).getAllByRole("columnheader").map((th) => th.textContent)).toEqual(["When", "Kind", "Detail"]);
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((r) => within(r).getAllByRole("cell").slice(1).map((c) => c.textContent))).toEqual([
      ["failover", "de-fra-01 → nl-ams-03 — real check failed 3×"],
      ["failover", "fi-hel-02 → de-fra-01 — real check failed 3×"],
    ]);
    expect(within(rows[0]!).getAllByRole("cell")[0]).toHaveTextContent(clock((NOW_SEC - 600) * 1000));
    expect(within(within(card).getByRole("list", { name: "Failovers" })).getAllByRole("listitem")).toHaveLength(2);
  });

  it("H5: falls back to last_failover_at when no event matches, and says when there is none", async () => {
    const api$ = mockApi();
    api$.getNetwork.mockResolvedValue({ ...NETWORK, events: [{ ts: NOW_SEC - 5, kind: "apply", detail: "applied" }] });
    api$.getStatus.mockResolvedValue({ ...STATUS, last_failover_at: NOW_SEC - 700 });
    const { client } = renderApp("/traffic");
    const card = await screen.findByRole("region", { name: "Failover history" });
    expect(await within(card).findByRole("table")).toHaveTextContent("Auto-failover — the gateway switched node");

    api$.getStatus.mockResolvedValue({ ...STATUS, last_failover_at: null });
    await act(() => client.refetchQueries({ queryKey: ["status"] }));
    expect(await within(card).findByText("No failovers recorded.")).toBeInTheDocument();
  });

  it("a failed network read shows Retry in the history card while the charts keep rendering", async () => {
    const api$ = mockApi();
    api$.getNetwork.mockRejectedValue(new ApiError(500, "boom"));
    const client = createQueryClient();
    client.setDefaultOptions({ queries: { ...client.getDefaultOptions().queries, retry: false } });
    renderApp("/traffic", { client });
    const card = await screen.findByRole("region", { name: "Failover history" });
    expect(await within(card).findByRole("alert")).toHaveTextContent("Failover history did not load");
    expect(await within(region("Probe latency by node")).findByRole("list", { name: "Nodes by latency" })).toBeInTheDocument();
  });

  it("waits with a skeleton, not the empty message", async () => {
    const api$ = mockApi();
    api$.getNetwork.mockReturnValue(new Promise(() => {}));
    renderApp("/traffic");
    expect(await screen.findByRole("region", { name: "Failover history" })).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByText("No failovers recorded.")).toBeNull();
  });
});
