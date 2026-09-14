import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, type Status, type TrafficFrame } from "../../api/client";
import { closePalette } from "../../app/shell/palette";
import { settleConfirm } from "../../components/confirm";
import { NETWORK, NOW_SEC, STATUS, SUBS, TRAFFIC_FRAME, holdConnectionWrite, mockApi } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";

afterEach(() => {
  act(() => { settleConfirm(false); closePalette(); });
  localStorage.clear();
  vi.useRealTimers();
});

/** On fake timers Testing Library's findBy never polls: step the clock until `check` holds. */
async function stepUntil(check: () => unknown) {
  for (let i = 0; i < 80 && !check(); i++) await act(() => vi.advanceTimersByTimeAsync(25));
  expect(check()).toBeTruthy();
}

const FAKE_TIMERS = { toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] } as const;

async function openOverview(status: Partial<Status> = {}) {
  const api$ = mockApi();
  api$.getStatus.mockResolvedValue({ ...STATUS, ...status });
  const view = renderApp("/");
  const block = await screen.findByRole("region", { name: "Status" });
  await within(block).findByText(/^(\S+ )?nl-ams-03$|^No node$/);
  return { api$, block, ...view };
}

const withDirect = (up: number, down: number): TrafficFrame => ({
  ...TRAFFIC_FRAME, outbounds: { ...TRAFFIC_FRAME.outbounds, direct: { up_bps: up, down_bps: down } },
});

describe("Overview › alerts", () => {
  it("O1: config drift is an undismissable alert whose Reload config re-applies the active node", async () => {
    const { client } = await openOverview({ config_drift: "drift" });
    const apply = vi.spyOn(api, "apply").mockResolvedValue({ ok: true });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await screen.findByRole("alert", { name: "Config drift" });
    const alert = screen.getByRole("group", { name: "Config drift" });
    expect(within(alert).queryByRole("button", { name: /Dismiss/ })).toBeNull();
    expect(within(screen.getByRole("region", { name: "Status" })).getByText("stale config")).toBeInTheDocument();
    await userEvent.click(within(alert).getByRole("button", { name: "Reload config" }));
    await waitFor(() => expect(apply).toHaveBeenCalledWith(1));
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["status"] }));
  });

  it("O1: unknown drift, or drift with no active node, shows nothing", async () => {
    const { api$, client } = await openOverview({ config_drift: "unknown" });
    expect(screen.queryByRole("alert", { name: "Config drift" })).toBeNull();
    api$.getStatus.mockResolvedValue({ ...STATUS, config_drift: "drift", active_node_id: null, tunnel_online: false });
    await act(() => client.refetchQueries({ queryKey: ["status"] }));
    await within(screen.getByRole("region", { name: "Status" })).findByText("No node");
    expect(screen.queryByRole("alert", { name: "Config drift" })).toBeNull();
  });

  it("O2: a recent failover is announced; dismissing it is remembered, and a newer one shows again", async () => {
    const { api$, client } = await openOverview({ last_failover_at: NOW_SEC - 600 });
    await screen.findByRole("status", { name: "Auto-failover" });
    const banner = screen.getByRole("group", { name: "Auto-failover" });
    expect(banner).toHaveTextContent("Auto-failover to nl-ams-03 · 10m ago");
    expect(within(banner).getByText("to nl-ams-03 · 10m ago")).toHaveAttribute("aria-live", "off");
    await userEvent.click(within(banner).getByRole("button", { name: "Dismiss: Auto-failover" }));
    expect(screen.queryByRole("group", { name: "Auto-failover" })).toBeNull();
    expect(localStorage.getItem("failoverDismissed")).toBe(String(NOW_SEC - 600));

    api$.getStatus.mockResolvedValue({ ...STATUS, last_failover_at: NOW_SEC - 60 });
    await act(() => client.refetchQueries({ queryKey: ["status"] }));
    await screen.findByRole("status", { name: "Auto-failover" });
    expect(screen.getByRole("group", { name: "Auto-failover" })).toHaveTextContent("to nl-ams-03 · 1m ago");
  });

  it("O2: a dismissal stored by the previous panel keeps that failover hidden", async () => {
    localStorage.setItem("failoverDismissed", String(NOW_SEC - 600));
    await openOverview({ last_failover_at: NOW_SEC - 600 });
    expect(screen.queryByRole("status", { name: "Auto-failover" })).toBeNull();
  });

  it("O2: a failover older than a day is not announced", async () => {
    await openOverview({ last_failover_at: NOW_SEC - 90_000 });
    expect(screen.queryByRole("status", { name: "Auto-failover" })).toBeNull();
  });

  it("O3: subscription expiry and data-cap warnings for enabled subscriptions only", async () => {
    await openOverview();
    await screen.findByRole("status", { name: "Subscriptions" });
    const banner = screen.getByRole("group", { name: "Subscriptions" });
    expect(within(banner).getByText("work: expires in 2d")).toBeInTheDocument();
    expect(within(banner).getByText("home: 86% of data cap")).toBeInTheDocument();
    expect(banner).not.toHaveTextContent("old");
  });

  it("O3: an expired subscription makes the banner an alert", async () => {
    const api$ = mockApi();
    api$.listSubs.mockResolvedValue([{ ...SUBS[0]!, expire_at: NOW_SEC - 10 }]);
    renderApp("/");
    await screen.findByRole("alert", { name: "Subscriptions" });
    expect(screen.getByRole("group", { name: "Subscriptions" })).toHaveTextContent("work: expired");
  });

  it("O5: untunneled traffic raises the banner only above 50 000 bps", async () => {
    const { api$ } = await openOverview();
    api$.emitTraffic(withDirect(10_000, 40_000));
    expect(screen.queryByRole("status", { name: "Traffic bypassing the tunnel" })).toBeNull();
    api$.emitTraffic(withDirect(20_000, 100_000));
    expect(screen.getByRole("status", { name: "Traffic bypassing the tunnel" })).toHaveTextContent(/^Traffic bypassing the tunnel$/);
    const rate = within(screen.getByRole("group", { name: "Traffic bypassing the tunnel" })).getByText("· 120 kbit/s");
    expect(rate).toHaveAttribute("aria-live", "off");
    api$.emitTraffic(withDirect(30_000, 100_000));   // the rate changes; the live region does not
    expect(rate).toHaveTextContent("· 130 kbit/s");
    expect(screen.getByRole("status", { name: "Traffic bypassing the tunnel" })).toHaveTextContent(/^Traffic bypassing the tunnel$/);
  });

  it("nothing is wrong: no alerts at all", async () => {
    const api$ = mockApi();
    api$.listSubs.mockResolvedValue([]);
    renderApp("/");
    await screen.findByRole("region", { name: "Status" });
    await waitFor(() => expect(api$.listSubs).toHaveBeenCalled());
    expect(screen.queryByRole("status", { name: /Auto-failover|Subscriptions|bypassing/ })).toBeNull();
    expect(screen.queryByRole("alert", { name: "Config drift" })).toBeNull();
  });
});

describe("Overview › status block", () => {
  it("O4: tunnel, node with its flag and endpoint, xray, kill-switch, clients of the pool, ticking uptime", async () => {
    const { api$, block } = await openOverview();
    api$.emitTraffic(TRAFFIC_FRAME);
    expect(within(block).getByRole("img", { name: "42 ms · ONLINE" })).toBeInTheDocument();
    expect(within(block).getByText("Tunnel ONLINE")).toBeInTheDocument();
    expect(within(block).getByText("🇳🇱 nl-ams-03")).toBeInTheDocument();
    expect(within(block).getByText("VLESS · Reality · nl-ams-03.example.org:443")).toBeInTheDocument();
    expect(within(block).getByText("RUNNING").parentElement).toHaveAttribute("data-tone", "ok");
    await waitFor(() => expect(within(block).getByText("ARMED").parentElement).toHaveAttribute("data-tone", "ok"));
    expect(within(block).getByText("12 / 50")).toBeInTheDocument();
    expect(within(block).getByText(/^00:01:4\d$/)).toBeInTheDocument();
    expect(within(block).queryByText("stale config")).toBeNull();
  });

  it("O4: xray reconnecting, kill-switch open, and a stopped xray is an offline tunnel", async () => {
    const api$ = mockApi();
    api$.getStatus.mockResolvedValue({ ...STATUS, running: false, xray_state: "error", tunnel_online: false });
    api$.getNetwork.mockResolvedValue({ ...NETWORK, kill_switch_enabled: false });
    renderApp("/");
    const block = await screen.findByRole("region", { name: "Status" });
    expect(await within(block).findByText("RECONNECTING")).toBeInTheDocument();
    expect(await within(block).findByText("OPEN")).toBeInTheDocument();
    expect(within(block).getByText("Tunnel OFFLINE")).toBeInTheDocument();
    expect(within(block).getByRole("img", { name: "× OFFLINE" })).toBeInTheDocument();
    expect(within(block).getByText("—", { selector: "span" })).toBeInTheDocument();   // no uptime while not online
  });

  it("O4: xray running with an active node but health not fresh is unknown — in the block, the health pill and the topbar", async () => {
    const { block } = await openOverview({ tunnel_online: false, active_health_fresh: false });
    expect(within(block).getByText("Tunnel UNKNOWN")).toBeInTheDocument();
    expect(within(block).getByRole("img", { name: "— UNKNOWN" })).toBeInTheDocument();
    expect(await within(screen.getByRole("region", { name: "Upstream health" })).findByText("UNKNOWN")).toBeInTheDocument();
    expect(screen.getByText("Tunnel unknown")).toHaveClass("text-t2");
  });

  it("O4: a fresh failed probe of the active node turns every tunnel surface offline at once", async () => {
    const { api$, block } = await openOverview();
    expect(screen.getByText("Tunnel online")).toBeInTheDocument();
    api$.emitTraffic({ ...TRAFFIC_FRAME, active: { ...TRAFFIC_FRAME.active!, real_ok: false } });
    expect(within(block).getByText("Tunnel OFFLINE")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Upstream health" })).getByText("OFFLINE")).toBeInTheDocument();
    expect(screen.getByText("Tunnel offline")).toHaveClass("text-bad");
  });

  it("O4: with no active node the tunnel is offline and there is nothing to disconnect", async () => {
    const { block } = await openOverview({ active_node_id: null, tunnel_online: false, running: false, xray_state: "stopped" });
    expect(within(block).getByText("No node")).toBeInTheDocument();
    expect(within(block).getByRole("img", { name: "× OFFLINE" })).toBeInTheDocument();
    expect(within(block).getByText("STOPPED")).toBeInTheDocument();
    expect(within(block).queryByRole("button", { name: "Disconnect" })).toBeNull();
    expect(within(block).getByRole("button", { name: "Switch node" })).toBeInTheDocument();
  });

  it("offline: while the status poll fails the block keeps its last values, dimmed, and the tunnel is unknown", async () => {
    const { api$, block, client } = await openOverview();
    api$.getStatus.mockRejectedValue(new ApiError(0, "network error"));
    await act(() => client.refetchQueries({ queryKey: ["status"] }));
    await waitFor(() => expect(block).toHaveAttribute("data-stale", "true"));
    expect(within(block).getByText("Tunnel UNKNOWN")).toBeInTheDocument();
    expect(within(block).getByText("nl-ams-03")).toBeInTheDocument();
  });

  it("shows a skeleton until the first status arrives", async () => {
    const api$ = mockApi();
    api$.getStatus.mockReturnValue(new Promise<Status>(() => {}));
    renderApp("/");
    expect(await screen.findByRole("region", { name: "Status" })).toHaveAttribute("aria-busy", "true");
  });

  it("Switch node opens the palette on the node list", async () => {
    await openOverview();
    await userEvent.click(screen.getByRole("button", { name: "Switch node" }));
    expect(await screen.findByPlaceholderText("Switch to node…")).toBeInTheDocument();
  });
});

describe("Overview › roll back and disconnect", () => {
  it("O12: no Roll back without a previous node, or when the previous node is the active one", async () => {
    const { api$, client, block } = await openOverview();
    expect(within(block).queryByRole("button", { name: /Roll back/ })).toBeNull();
    api$.getStatus.mockResolvedValue({ ...STATUS, prev_active_node_id: 1 });
    await act(() => client.refetchQueries({ queryKey: ["status"] }));
    expect(within(block).queryByRole("button", { name: /Roll back/ })).toBeNull();
  });

  it("O12: no Roll back when the gateway says it would not work, even with a previous node recorded", async () => {
    const { api$, client, block } = await openOverview({ prev_active_node_id: 2, rollback_available: false });
    expect(within(block).queryByRole("button", { name: /Roll back/ })).toBeNull();
    api$.getStatus.mockResolvedValue({ ...STATUS, prev_active_node_id: 2, rollback_available: undefined });
    await act(() => client.refetchQueries({ queryKey: ["status"] }));
    expect(within(block).queryByRole("button", { name: /Roll back/ })).toBeNull();
    api$.getStatus.mockResolvedValue({ ...STATUS, prev_active_node_id: 2 });
    await act(() => client.refetchQueries({ queryKey: ["status"] }));
    expect(await within(block).findByRole("button", { name: "Roll back to de-fra-01" })).toBeInTheDocument();
  });

  it.each([
    ["is no longer available", { rollback_available: false }],
    ["is another node", { prev_active_node_id: 3 }],
  ])("O12: a confirmed roll back whose target %s by then does not call the gateway", async (_what, change) => {
    const { client, block } = await openOverview({ prev_active_node_id: 2 });
    const rollback = vi.spyOn(api, "rollback").mockResolvedValue({ ok: true });
    const error = vi.spyOn(toast, "error");
    await userEvent.click(within(block).getByRole("button", { name: "Roll back to de-fra-01" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm" });
    act(() => { client.setQueryData<Status>(["status"], (old) => ({ ...old!, ...change })); });
    await userEvent.click(within(dialog).getByRole("button", { name: "Roll back" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("The rollback target changed — try again", { duration: 20000 }));
    expect(rollback).not.toHaveBeenCalled();
  });

  it("O12: a connection write that starts while the confirmation is open stops the roll back and the disconnect", async () => {
    const { client, block } = await openOverview({ prev_active_node_id: 2 });
    const rollback = vi.spyOn(api, "rollback").mockResolvedValue({ ok: true });
    const disconnect = vi.spyOn(api, "disconnect").mockResolvedValue({ ok: true });
    const error = vi.spyOn(toast, "error");
    await userEvent.click(within(block).getByRole("button", { name: "Roll back to de-fra-01" }));
    let dialog = await screen.findByRole("dialog", { name: "Confirm" });
    const release = holdConnectionWrite(client);
    await userEvent.click(within(dialog).getByRole("button", { name: "Roll back" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("Another connection change is still running — try again when it finishes", { duration: 20000 }));
    expect(rollback).not.toHaveBeenCalled();
    await release();

    await userEvent.click(within(block).getByRole("button", { name: "Disconnect" }));
    dialog = await screen.findByRole("dialog", { name: "Confirm" });
    const releaseAgain = holdConnectionWrite(client);
    await userEvent.click(within(dialog).getByRole("button", { name: "Disconnect" }));
    await waitFor(() => expect(error).toHaveBeenCalledTimes(2));
    expect(disconnect).not.toHaveBeenCalled();
    await releaseAgain();
  });

  it("O12: Roll back asks first, then rolls back and refreshes the connection state", async () => {
    const { client, block } = await openOverview({ prev_active_node_id: 2 });
    const rollback = vi.spyOn(api, "rollback").mockResolvedValue({ ok: true });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const success = vi.spyOn(toast, "success");
    await userEvent.click(within(block).getByRole("button", { name: "Roll back to de-fra-01" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm" });
    expect(dialog).toHaveTextContent("Roll back the live config to de-fra-01?");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(rollback).not.toHaveBeenCalled();

    await userEvent.click(within(block).getByRole("button", { name: "Roll back to de-fra-01" }));
    await userEvent.click(within(await screen.findByRole("dialog", { name: "Confirm" })).getByRole("button", { name: "Roll back" }));
    await waitFor(() => expect(rollback).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["status"] }));
    await waitFor(() => expect(success).toHaveBeenCalledWith("Rolled back to de-fra-01", { duration: 8000 }));
  });

  it("O12: a rollback the gateway had nothing for says so", async () => {
    const { block } = await openOverview({ prev_active_node_id: 2 });
    vi.spyOn(api, "rollback").mockResolvedValue({ ok: false });
    const success = vi.spyOn(toast, "success");
    await userEvent.click(within(block).getByRole("button", { name: "Roll back to de-fra-01" }));
    await userEvent.click(within(await screen.findByRole("dialog", { name: "Confirm" })).getByRole("button", { name: "Roll back" }));
    await waitFor(() => expect(success).toHaveBeenCalledWith("Nothing to roll back", { duration: 8000 }));
  });

  it("Disconnect asks first, then disconnects the active node", async () => {
    const { client, block } = await openOverview();
    const disconnect = vi.spyOn(api, "disconnect").mockResolvedValue({ ok: true });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await userEvent.click(within(block).getByRole("button", { name: "Disconnect" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm" });
    expect(dialog).toHaveTextContent("Disconnect from nl-ams-03?");
    await userEvent.click(within(dialog).getByRole("button", { name: "Disconnect" }));
    await waitFor(() => expect(disconnect).toHaveBeenCalledWith(1));
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["network"] }));
  });

  it("connection writes never overlap: while a Disconnect runs, Reload config, Roll back and xray-core wait", async () => {
    const { block } = await openOverview({ config_drift: "drift", prev_active_node_id: 2 });
    let finish: () => void = () => {};
    vi.spyOn(api, "disconnect").mockImplementation(() => new Promise((resolve) => { finish = () => resolve({ ok: true }); }));
    const reload = within(screen.getByRole("group", { name: "Config drift" })).getByRole("button", { name: "Reload config" });
    const rollback = within(block).getByRole("button", { name: "Roll back to de-fra-01" });
    expect(reload).toBeEnabled();
    expect(rollback).toBeEnabled();
    await userEvent.click(within(block).getByRole("button", { name: "Disconnect" }));
    await userEvent.click(within(await screen.findByRole("dialog", { name: "Confirm" })).getByRole("button", { name: "Disconnect" }));
    expect(await within(block).findByRole("button", { name: "Disconnecting…" })).toBeDisabled();
    expect(reload).toBeDisabled();
    expect(rollback).toBeDisabled();
    for (const toggle of screen.getAllByRole("switch", { name: "xray-core" })) expect(toggle).toBeDisabled();
    await act(async () => finish());
    await waitFor(() => expect(reload).toBeEnabled());
  });

  it("Reload config re-applies the node that is active when it is pressed", async () => {
    const { client } = await openOverview({ config_drift: "drift" });
    const apply = vi.spyOn(api, "apply").mockResolvedValue({ ok: true });
    const reload = within(await screen.findByRole("group", { name: "Config drift" })).getByRole("button", { name: "Reload config" });
    act(() => { client.setQueryData<Status>(["status"], (old) => ({ ...old!, active_node_id: 2 })); });
    await userEvent.click(reload);
    await waitFor(() => expect(apply).toHaveBeenCalledWith(2));
  });

  it("a failed roll back raises the backend's message", async () => {
    const { block } = await openOverview({ prev_active_node_id: 2 });
    vi.spyOn(api, "rollback").mockRejectedValue(new ApiError(409, "rollback target was revoked"));
    const error = vi.spyOn(toast, "error");
    await userEvent.click(within(block).getByRole("button", { name: "Roll back to de-fra-01" }));
    await userEvent.click(within(await screen.findByRole("dialog", { name: "Confirm" })).getByRole("button", { name: "Roll back" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("rollback target was revoked", { duration: 20000 }));
  });
});

describe("Overview › KPIs", () => {
  it("O5: live rates with a trend, session totals and the live chip", async () => {
    const { api$ } = await openOverview();
    api$.emitTraffic({ ...TRAFFIC_FRAME, ts: TRAFFIC_FRAME.ts - 1_000 });
    api$.emitTraffic(TRAFFIC_FRAME);
    const down = screen.getByRole("region", { name: "↓ Download" });
    expect(down).toHaveTextContent("12.4Mbit/s");
    expect(down.querySelector("svg path")).not.toBeNull();
    expect(screen.getByRole("region", { name: "↑ Upload" })).toHaveTextContent("1.8Mbit/s");
    const sessionDown = screen.getByRole("region", { name: "↓ Session total" });
    expect(sessionDown).toHaveTextContent("18.60GB");
    expect(sessionDown).toHaveTextContent("since the last connect");
    expect(within(sessionDown).getByText("live")).toHaveAttribute("data-tone", "ok");
    expect(screen.getByRole("region", { name: "↑ Session total" })).toHaveTextContent("1.24GB");
  });

  it("connecting… until a frame arrives; five seconds without one dims the KPIs, the orb and the path's live values", async () => {
    vi.useFakeTimers({ toFake: [...FAKE_TIMERS.toFake] });
    const api$ = mockApi();
    renderApp("/");
    await stepUntil(() => screen.queryByRole("region", { name: "Connection path" })?.querySelector("[data-live]"));
    const kpis = ["↓ Download", "↑ Upload", "↓ Session total", "↑ Session total"].map((name) => screen.getByRole("region", { name }));
    const sessionDown = kpis[2]!;
    expect(within(sessionDown).getByText("connecting…")).toHaveAttribute("data-tone", "neutral");

    api$.emitTraffic(TRAFFIC_FRAME);
    expect(within(sessionDown).getByText("live")).toHaveAttribute("data-tone", "ok");
    for (const kpi of kpis) expect(kpi).not.toHaveAttribute("data-dim");
    const orb = within(screen.getByRole("region", { name: "Status" })).getByRole("img", { name: "42 ms · ONLINE" });
    const live = screen.getByRole("region", { name: "Connection path" }).querySelector("[data-live]")!;
    expect(orb).not.toHaveAttribute("data-dim");
    expect(live).not.toHaveAttribute("data-dim");

    await act(() => vi.advanceTimersByTimeAsync(6_000));
    expect(within(sessionDown).getByText("connecting…")).toBeInTheDocument();
    for (const kpi of kpis) expect(kpi).toHaveAttribute("data-dim", "true");
    expect(orb).toHaveAttribute("data-dim", "true");
    expect(live).toHaveAttribute("data-dim", "true");
    expect(live).toHaveTextContent("Node · 42 ms · egress");   // still shown, just not as live

    api$.emitTraffic(TRAFFIC_FRAME);
    expect(within(sessionDown).getByText("live")).toBeInTheDocument();
    expect(orb).not.toHaveAttribute("data-dim");
  });

  it("a frozen failed probe stops deciding the tunnel once frames stop", async () => {
    vi.useFakeTimers({ toFake: [...FAKE_TIMERS.toFake] });
    const api$ = mockApi();
    renderApp("/");
    await stepUntil(() => screen.queryByText("Tunnel online"));
    api$.emitTraffic({ ...TRAFFIC_FRAME, active: { ...TRAFFIC_FRAME.active!, real_ok: false } });
    expect(screen.getByText("Tunnel offline")).toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(6_000));
    expect(screen.getByText("Tunnel online")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Status" })).getByText("Tunnel ONLINE")).toBeInTheDocument();
  });

  it("stats off: every KPI reads —, and the chip says so", async () => {
    const { api$ } = await openOverview();
    api$.emitTraffic(TRAFFIC_FRAME);
    api$.emitTraffic({ disabled: true });
    for (const name of ["↓ Download", "↑ Upload", "↓ Session total", "↑ Session total"]) {
      expect(screen.getByRole("region", { name })).toHaveTextContent("—");
    }
    expect(screen.getByText("stats off")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "↓ Download" }).querySelector("svg")).toBeNull();
  });
});
