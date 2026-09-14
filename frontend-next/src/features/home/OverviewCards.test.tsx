import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { ApiError, type Routing, type Status, type TrafficFrame } from "../../api/client";
import { createQueryClient } from "../../api/queryClient";
import { closePalette } from "../../app/shell/palette";
import { NETWORK, NODE_HEALTH, STATUS, TRAFFIC_FRAME, mockApi } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";

afterEach(() => act(() => closePalette()));

/** A client that gives up on the first failure, so error states show without waiting out a retry. */
function noRetryClient() {
  const client = createQueryClient();
  client.setDefaultOptions({ queries: { ...client.getDefaultOptions().queries, retry: false } });
  return client;
}

async function openOverview(status: Partial<Status> = {}) {
  const api$ = mockApi();
  api$.getStatus.mockResolvedValue({ ...STATUS, ...status });
  const view = renderApp("/");
  await screen.findByRole("region", { name: "Status" });
  await within(await screen.findByRole("region", { name: "Upstream health" })).findByRole("list", { name: "Standby nodes" });
  return { api$, ...view };
}

const region = (name: string) => screen.getByRole("region", { name });
const frame = (patch: Partial<NonNullable<TrafficFrame["active"]>>, ts = TRAFFIC_FRAME.ts): TrafficFrame => ({
  ...TRAFFIC_FRAME, ts, active: { ...TRAFFIC_FRAME.active!, ...patch },
});

describe("Overview › layout", () => {
  it("one column on a phone in the approved order, the xray-core switch last", async () => {
    await openOverview();
    const main = screen.getByRole("main");
    const names = within(main).getAllByRole("region").map((r) => r.getAttribute("aria-label"));
    expect(names).toEqual([
      "Status", "↓ Download", "↑ Upload", "↓ Session total", "↑ Session total",
      "Throughput", "Upstream health", "Connection path", "Events", "Routing", "Network",
    ]);
    const grid = within(main).getByRole("region", { name: "Status" }).parentElement!;
    expect(grid.lastElementChild).toContainElement(within(grid).getByRole("switch", { name: "xray-core" }));
    expect(grid.lastElementChild).toHaveClass("md:hidden");
  });

  it("the phone xray-core card lives on Overview only; the sidebar keeps its own", async () => {
    const api$ = mockApi();
    const { router } = renderApp("/");
    await screen.findByRole("region", { name: "Status" });
    expect(screen.getAllByRole("switch", { name: "xray-core" })).toHaveLength(2);
    await act(() => router.navigate({ to: "/nodes" }));
    await screen.findByText("Nodes › Servers");
    expect(screen.getAllByRole("switch", { name: "xray-core" })).toHaveLength(1);
    expect(api$.getStatus).toHaveBeenCalled();
  });
});

describe("Overview › traffic chart", () => {
  it("O6: plots the live 10-minute window with its peak", async () => {
    const { api$ } = await openOverview();
    api$.emitTraffic({ ...TRAFFIC_FRAME, ts: TRAFFIC_FRAME.ts - 2_000, outbounds: { proxy: { up_bps: 1, down_bps: 18_200_000 } } });
    api$.emitTraffic(TRAFFIC_FRAME);
    const card = region("Throughput");
    expect(within(card).getByRole("img", { name: "Throughput over the last 10m" })).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "10m" })).toHaveAttribute("aria-pressed", "true");
    expect(card.querySelector("[data-peak]")).toHaveTextContent("peak 18.2 Mbit/s");
    expect(within(card).queryByText(/Tunnel health is stale/)).toBeNull();
  });

  it("O6: 24h switches to recorded history, and back", async () => {
    const { api$ } = await openOverview();
    api$.emitTraffic(TRAFFIC_FRAME);
    api$.getTrafficHistory.mockResolvedValue({ samples: [[TRAFFIC_FRAME.ts - 60_000, 5, 7_000_000], [TRAFFIC_FRAME.ts, 5, 9_000_000]], interval_ms: 60_000 });
    const card = region("Throughput");
    await userEvent.click(within(card).getByRole("button", { name: "24h" }));
    await waitFor(() => expect(api$.getTrafficHistory.mock.calls.some(([sec]) => sec === 86_400)).toBe(true));
    expect(await within(card).findByRole("img", { name: "Throughput over the last 24h" })).toBeInTheDocument();
    expect(card.querySelector("[data-peak]")).toHaveTextContent("peak 9.0 Mbit/s");
    await userEvent.click(within(card).getByRole("button", { name: "10m" }));
    expect(within(card).getByRole("button", { name: "10m" })).toHaveAttribute("aria-pressed", "true");
  });

  it("O6: stale health dims the chart with its caption", async () => {
    const { api$ } = await openOverview();
    api$.emitTraffic(frame({ stale: true }, TRAFFIC_FRAME.ts - 1_000));
    api$.emitTraffic(frame({ stale: true }));
    expect(within(region("Throughput")).getByRole("status")).toHaveTextContent("Tunnel health is stale — rates are not proof of a healthy tunnel");
  });

  it("O6: with no active node there is no tunnel to judge — no stale caption, no dimming", async () => {
    const { api$ } = await openOverview({ active_node_id: null, tunnel_online: false });
    api$.emitTraffic({ ...TRAFFIC_FRAME, ts: TRAFFIC_FRAME.ts - 1_000, active: null });
    api$.emitTraffic({ ...TRAFFIC_FRAME, active: null });
    const card = region("Throughput");
    expect(within(card).getByRole("img", { name: "Throughput over the last 10m" })).toBeInTheDocument();
    expect(card.querySelector("[data-stale]")).toBeNull();
    expect(within(card).queryByText(/Tunnel health is stale/)).toBeNull();
  });

  it("O6: the recorded 24 h window is never dimmed by the current probe", async () => {
    const { api$ } = await openOverview();
    api$.emitTraffic(frame({ stale: true }));
    api$.getTrafficHistory.mockResolvedValue({ samples: [[TRAFFIC_FRAME.ts - 60_000, 5, 7_000_000], [TRAFFIC_FRAME.ts, 5, 9_000_000]], interval_ms: 60_000 });
    const card = region("Throughput");
    expect(within(card).getByText(/Tunnel health is stale/)).toBeInTheDocument();   // the live window is judged
    await userEvent.click(within(card).getByRole("button", { name: "24h" }));
    expect(await within(card).findByRole("img", { name: "Throughput over the last 24h" })).toBeInTheDocument();
    expect(card.querySelector("[data-stale]")).toBeNull();
    expect(within(card).queryByText(/Tunnel health is stale/)).toBeNull();
  });

  it("stats off: the chart says so and points to System › Panel", async () => {
    const { api$ } = await openOverview();
    api$.emitTraffic({ disabled: true });
    const card = region("Throughput");
    expect(within(card).getByText("Traffic stats are off")).toBeInTheDocument();
    expect(within(card).getByRole("link", { name: "System › Panel" })).toHaveAttribute("href", expect.stringContaining("/system/panel"));
    expect(within(card).queryByRole("img")).toBeNull();
  });
});

describe("Overview › upstream health", () => {
  it("O7: the active node live, four standbys by latency with their age, and FAILOVER READY", async () => {
    const { api$ } = await openOverview();
    api$.emitTraffic(TRAFFIC_FRAME);
    const card = region("Upstream health");
    expect(within(card).getByText("FAILOVER READY")).toBeInTheDocument();
    expect(within(within(card).getByRole("list", { name: "Active node" })).getByRole("listitem")).toHaveTextContent("🇳🇱 nl-ams-0342 mslive");
    expect(within(within(card).getByRole("list", { name: "Standby nodes" })).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "🇩🇪 de-fra-0158 ms· 4 min", "🇫🇮 fi-hel-0271 ms· 6 min", "🇵🇱 pl-waw-01164 ms· 23 min", "🇸🇪 se-sto-01failed· 12 min",
    ]);
  });

  it("O7: probe failed and health stale on the active row; NO ELIGIBLE STANDBY", async () => {
    const { api$, client } = await openOverview({ failover_ready: false });
    const active = () => within(within(region("Upstream health")).getByRole("list", { name: "Active node" })).getByRole("listitem");
    expect(active()).toHaveTextContent("health stale");
    expect(within(region("Upstream health")).getByText("NO ELIGIBLE STANDBY")).toBeInTheDocument();
    api$.emitTraffic(frame({ real_ok: false }));
    expect(active()).toHaveTextContent("probe failed");
    expect(within(region("Upstream health")).getByText("OFFLINE")).toBeInTheDocument();   // a fresh failed probe
    api$.getStatus.mockResolvedValue({ ...STATUS, tunnel_online: false, active_node_id: null });
    await act(() => client.refetchQueries({ queryKey: ["status"] }));
    expect(await within(region("Upstream health")).findByText("OFFLINE")).toBeInTheDocument();
  });

  it("O7: a standby never probed reads 'not probed' once there is room for it", async () => {
    const api$ = mockApi();
    api$.listNodeHealth.mockResolvedValue(NODE_HEALTH.filter((h) => h.node_id !== 4 && h.node_id !== 6));
    renderApp("/");
    const standby = await within(await screen.findByRole("region", { name: "Upstream health" })).findByRole("list", { name: "Standby nodes" });
    expect(within(standby).getAllByRole("listitem").at(-1)).toHaveTextContent("not probed");
  });

  it("without nodes it says where to add one", async () => {
    const api$ = mockApi();
    api$.listNodes.mockResolvedValue([]);
    api$.getStatus.mockResolvedValue({ ...STATUS, active_node_id: null, tunnel_online: false });
    renderApp("/");
    const card = await screen.findByRole("region", { name: "Upstream health" });
    expect(await within(card).findByRole("link", { name: "Nodes › Servers" })).toBeInTheDocument();
  });
});

describe("Overview › connection path, events and summaries", () => {
  it("a live frame about another node lends none of its latency, egress or health to the active one", async () => {
    const { api$ } = await openOverview();
    api$.emitTraffic(frame({ node_id: 2 }));
    const status = region("Status");
    expect(within(status).getByRole("img", { name: "— ONLINE" })).toBeInTheDocument();
    expect(within(status).getByText("nl-ams-03")).toBeInTheDocument();   // no flag from the other node's egress
    const path = region("Connection path");
    expect(path).toHaveTextContent("Node · egress—");
    expect(path).not.toHaveTextContent("185.107.56.21");
    expect(path.querySelector("path[data-leg]")).toHaveAttribute("data-leg", "off");
    const active = within(within(region("Upstream health")).getByRole("list", { name: "Active node" })).getByRole("listitem");
    expect(active).toHaveTextContent("health stale");
    expect(within(region("Throughput")).getByRole("status")).toHaveTextContent("Tunnel health is stale");
  });

  it("O8: the path from the network read and the live frame; the bypass turns amber with any direct traffic", async () => {
    const { api$ } = await openOverview();
    api$.emitTraffic(TRAFFIC_FRAME);
    const card = region("Connection path");
    expect(within(card).getByRole("img")).toHaveAccessibleName("Connection path: devices, gateway, nl-ams-03, internet. Tunnel leg OK; bypass idle.");
    expect(card).toHaveTextContent("Devices · 12 clientspool 50");
    expect(card).toHaveTextContent("Gateway10.0.2.1 · eth0.2");
    expect(card).toHaveTextContent("Node · 42 ms · egress185.107.56.212a0b:4d07::21");
    expect(card).toHaveTextContent("Internet · uplinkv4 ✓ · v6 ✓");
    expect(within(card).getByText("ARMED")).toBeInTheDocument();
    api$.emitTraffic({ ...TRAFFIC_FRAME, outbounds: { ...TRAFFIC_FRAME.outbounds, direct: { up_bps: 0, down_bps: 800 } } });
    expect(card.querySelector("path[data-bypass]")).toHaveAttribute("data-bypass", "leaking");
    expect(screen.queryByRole("status", { name: "Traffic bypassing the tunnel" })).toBeNull();   // the alert waits for 50 kbit/s
  });

  it("O11: the last six events, newest first, with levels and a link to the logs", async () => {
    await openOverview();
    const card = region("Events");
    const rows = within(within(card).getByRole("list", { name: "Recent events" })).getAllByRole("listitem");
    expect(rows.map((r) => [r.querySelector("[data-level]")!.textContent, r.querySelector("b")!.textContent])).toEqual([
      ["bad", "leak"], ["info", "health"], ["bad", "failover"], ["warn", "retry"], ["info", "subscription"], ["bad", "failover"],
    ]);
    expect(card).not.toHaveTextContent("config applied for fi-hel-02");
    expect(within(card).getByRole("link", { name: /View all/ })).toHaveAttribute("href", expect.stringContaining("/system/logs"));
  });

  it("O9: the first four enabled rules, the count, and the default towards the active node", async () => {
    await openOverview();
    const card = region("Routing");
    expect(card).toHaveTextContent("· 4 of 6 rules");
    expect([...card.querySelectorAll("li:not([data-default])")].map((li) => li.textContent)).toEqual([
      "proxydomain:netflix.com", "directgeoip:ru", "blockgeosite:category-ads-all", "directdomain:gosuslugi.ru",
    ]);
    expect(card.querySelector("li[data-default]")).toHaveTextContent("default:proxy→ nl-ams-03");
    expect(within(card).getByRole("link", { name: /Tunnel › Routing/ })).toHaveAttribute("href", expect.stringContaining("/tunnel/routing"));
  });

  it("O9: no enabled rules still shows the default", async () => {
    const api$ = mockApi();
    api$.getRouting.mockResolvedValue({ default_action: "direct", domain_strategy: "AsIs", rules: [] } satisfies Routing);
    renderApp("/");
    const card = await screen.findByRole("region", { name: "Routing" });
    expect(await within(card).findByText("No enabled rules.")).toBeInTheDocument();
    expect(card.querySelector("li[data-default]")).toHaveTextContent(/^default:direct$/);   // only a proxy default names the node
  });

  it("O10: segment, DHCP pool with its size, client DNS and the IPv6 source", async () => {
    await openOverview();
    const card = region("Network");
    const values = within(card).getAllByRole("definition").map((d) => d.textContent);
    expect(values).toEqual(["10.0.2.1 · eth0.2", "10.0.2.100–10.0.2.149", "12 clients · pool 50", "10.0.2.1", "static"]);
    expect(within(card).getByRole("link", { name: /Gateway › Network/ })).toHaveAttribute("href", expect.stringContaining("/gateway/network"));
  });
});

describe("Overview › naming the active node", () => {
  it("before the node list loads every surface says node #N, the same way", async () => {
    const api$ = mockApi();
    api$.listNodes.mockReturnValue(new Promise(() => {}));
    api$.getStatus.mockResolvedValue({ ...STATUS, last_failover_at: STATUS.server_now - 600 });
    renderApp("/");
    const status = await screen.findByRole("region", { name: "Status" });
    expect(await within(status).findByText("node #1")).toBeInTheDocument();
    await screen.findByRole("status", { name: "Auto-failover" });
    expect(screen.getByRole("group", { name: "Auto-failover" })).toHaveTextContent("to node #1 · 10m ago");
    await within(region("Routing")).findByText("domain:netflix.com");
    expect(region("Routing").querySelector("li[data-default]")).toHaveTextContent("default:proxy→ node #1");
    await waitFor(() => expect(within(region("Connection path")).getByRole("img")).toHaveAccessibleName(/gateway, node #1, internet/));
  });

  it("with no active node every surface says No node", async () => {
    const api$ = mockApi();
    api$.getStatus.mockResolvedValue({ ...STATUS, active_node_id: null, tunnel_online: false });
    renderApp("/");
    const status = await screen.findByRole("region", { name: "Status" });
    expect(await within(status).findByText("No node")).toBeInTheDocument();
    await within(region("Routing")).findByText("domain:netflix.com");
    expect(region("Routing").querySelector("li[data-default]")).toHaveTextContent("default:proxy→ No node");
    await waitFor(() => expect(within(region("Connection path")).getByRole("img")).toHaveAccessibleName(/gateway, No node, internet/));
  });
});

describe("Overview › card states", () => {
  it("each card waits with a skeleton, never an empty message", async () => {
    const api$ = mockApi();
    api$.getNetwork.mockReturnValue(new Promise(() => {}));
    api$.getRouting.mockReturnValue(new Promise(() => {}));
    renderApp("/");
    for (const name of ["Connection path", "Events", "Routing", "Network"]) {
      expect(await screen.findByRole("region", { name })).toHaveAttribute("aria-busy", "true");
    }
    expect(screen.queryByText("No recent events.")).toBeNull();
    expect(screen.queryByText("No enabled rules.")).toBeNull();
  });

  it("a failed read shows Retry in its own cards while the others keep rendering", async () => {
    const api$ = mockApi();
    api$.getNetwork.mockRejectedValue(new ApiError(500, "boom"));
    renderApp("/", { client: noRetryClient() });
    const events = await screen.findByRole("region", { name: "Events" });
    expect(await within(events).findByRole("alert")).toHaveTextContent("Events did not load");
    expect(within(region("Connection path")).getByRole("alert")).toHaveTextContent("Network status did not load");
    expect(within(region("Network")).getByRole("alert")).toHaveTextContent("Network settings did not load");
    expect(await within(region("Routing")).findByText("domain:netflix.com")).toBeInTheDocument();

    api$.getNetwork.mockResolvedValue(NETWORK);
    await userEvent.click(within(events).getByRole("button", { name: "Retry" }));
    expect(await within(region("Events")).findByRole("list", { name: "Recent events" })).toBeInTheDocument();
  });

  it("a failed refresh keeps the last data and says so", async () => {
    const api$ = mockApi();
    const client = noRetryClient();
    renderApp("/", { client });
    const card = await screen.findByRole("region", { name: "Routing" });
    await within(card).findByText("domain:netflix.com");
    api$.getRouting.mockRejectedValue(new ApiError(500, "boom"));
    await act(() => client.refetchQueries({ queryKey: ["routing"] }));
    expect(await within(card).findByRole("status")).toHaveTextContent("Routing did not refresh — showing the last data");
    expect(within(card).queryByRole("alert")).toBeNull();   // a polite note: the card still has data
    expect(within(card).getByText("domain:netflix.com")).toBeInTheDocument();
  });

  it("upstream health: an error when node health cannot be read", async () => {
    const api$ = mockApi();
    api$.listNodeHealth.mockRejectedValue(new ApiError(500, "boom"));
    renderApp("/", { client: noRetryClient() });
    const card = await screen.findByRole("region", { name: "Upstream health" });
    expect(await within(card).findByRole("alert")).toHaveTextContent("Node health did not load");
  });
});
