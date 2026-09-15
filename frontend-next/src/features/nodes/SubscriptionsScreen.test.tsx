import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type RefreshAllResult, type RefreshResult, type Settings, type Subscription } from "../../api/client";
import { CONNECTION_BUSY, isConnectionBusy, isSettingsBusy } from "../../api/invalidation";
import { settleConfirm } from "../../components/confirm";
import { NOW_SEC, REFRESH_ALL, SETTINGS, STATUS, SUBS, holdConnectionWrite, mockApi } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";

afterEach(() => act(() => settleConfirm(false)));

const card = (name: string) => screen.getByRole("region", { name });

async function openSubscriptions() {
  const api$ = mockApi();
  const view = renderApp("/nodes/subscriptions");
  await screen.findByRole("region", { name: "work" });
  return { api$, ...view };
}

describe("Subscriptions › list (U1)", () => {
  it("each subscription: quota bar and expiry, URL, node count link, auto-update, last status, fetched ago", async () => {
    await openSubscriptions();
    const work = card("work");
    expect(within(work).getByText("#1")).toBeInTheDocument();
    expect(within(work).getByRole("meter", { name: "Data used by work" })).toHaveAttribute("aria-valuenow", "20");
    expect(work).toHaveTextContent(`20 GB / 100 GB · exp ${new Date((NOW_SEC + 2 * 86_400) * 1000).toISOString().slice(0, 10)}`);
    expect(within(work).getByRole("button", { name: "URL of work" })).toHaveAttribute("title", SUBS[0]!.url);
    expect(within(work).getByRole("link", { name: "6 nodes →" })).toHaveAttribute("href", "/nodes?group=1");
    expect(work).toHaveTextContent("every 60 min");
    expect(work).toHaveTextContent("ok: +0 ~6 -0 (tunnel)");
    expect(work).toHaveTextContent("fetched 4 min ago");
    expect(work).not.toHaveAttribute("data-paused");
    expect(screen.getByText("3 subscriptions")).toBeInTheDocument();
  });

  it("the URL opens in full on a tap, and a failed refresh shows its ⚠ with the whole error on a tap", async () => {
    await openSubscriptions();
    const url = within(card("work")).getByRole("button", { name: "URL of work" });
    expect(url).toHaveClass("truncate");
    await userEvent.click(url);
    expect(url).toHaveAttribute("aria-expanded", "true");
    expect(url).toHaveClass("break-all");

    const home = card("home");
    expect(home).toHaveTextContent("86 GB / 100 GB");
    const warning = within(home).getByRole("button", { name: "Last error of home" });
    expect(warning).toHaveAttribute("title", "fetch failed: timeout");
    await userEvent.click(warning);
    expect(within(home).getByText("fetch failed: timeout", { selector: "p" })).toBeInTheDocument();
    expect(home).toHaveTextContent("every 360 min");
  });

  it("a paused subscription is dimmed with its chip, auto-update off", async () => {
    await openSubscriptions();
    const old = card("old");
    expect(old).toHaveAttribute("data-paused", "true");
    expect(old).toHaveClass("opacity-60");
    expect(within(old).getByText("paused")).toBeInTheDocument();
    expect(old).toHaveTextContent("off");
    expect(within(old).queryByRole("meter")).toBeNull();
    expect(within(old).getByRole("button", { name: "Resume" })).toBeInTheDocument();
  });

  it("no subscriptions yet says how to start", async () => {
    const api$ = mockApi();
    api$.listSubs.mockResolvedValue([]);
    renderApp("/nodes/subscriptions");
    expect(await screen.findByText("No subscriptions yet — add one")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh all" })).toBeDisabled();
  });

  it("a list that failed to load is an error with Retry", async () => {
    const api$ = mockApi();
    api$.listSubs.mockRejectedValue(new ApiError(500, "boom"));
    renderApp("/nodes/subscriptions");
    expect(await screen.findByText("Subscriptions did not load", {}, { timeout: 3000 })).toBeInTheDocument();
  });
});

describe("Subscriptions › fetch settings (G1)", () => {
  it("shows both settings and saves each on its own, at once", async () => {
    const { api$ } = await openSubscriptions();
    const tunnel = await screen.findByRole("switch", { name: "Fetch subscriptions through the tunnel" });
    await waitFor(() => expect(tunnel).toBeChecked());
    expect(screen.getByRole("region", { name: "Subscription fetching" })).toHaveTextContent("never switches to weaker security; off keeps switching manual");
    let finish: (settings: Settings) => void = () => {};
    api$.putSettings.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    await userEvent.click(tunnel);
    expect(tunnel).not.toBeChecked();   // optimistic
    expect(api$.putSettings).toHaveBeenCalledWith({ tunneled_fetch: false });
    await act(async () => finish({ ...SETTINGS, tunneled_fetch: false }));
    expect(tunnel).not.toBeChecked();
  });

  it("a failed save puts the switch back and says so", async () => {
    const { api$ } = await openSubscriptions();
    api$.putSettings.mockRejectedValue(new ApiError(422, "subs_auto_switch: invalid"));
    const error = vi.spyOn(toast, "error");
    const autoSwitch = await screen.findByRole("switch", { name: "Subscription auto-switch" });
    await waitFor(() => expect(autoSwitch).toBeChecked());
    await userEvent.click(autoSwitch);
    await waitFor(() => expect(error).toHaveBeenCalledWith("subs_auto_switch: invalid", { duration: 20000 }));
    expect(autoSwitch).toBeChecked();
    expect(api$.putSettings).toHaveBeenCalledWith({ subs_auto_switch: false });
  });

  it("both switches are disabled while a save is out, then re-enabled with the saved value", async () => {
    const { api$ } = await openSubscriptions();
    const tunnel = await screen.findByRole("switch", { name: "Fetch subscriptions through the tunnel" });
    const autoSwitch = screen.getByRole("switch", { name: "Subscription auto-switch" });
    await waitFor(() => expect(tunnel).toBeChecked());
    let finish: (settings: Settings) => void = () => {};
    api$.putSettings.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    await userEvent.click(tunnel);
    expect(tunnel).toBeDisabled();
    expect(autoSwitch).toBeDisabled();   // the other row's switch is held too — one write at a time
    const saved: Settings = { ...SETTINGS, tunneled_fetch: false };
    api$.getSettings.mockResolvedValue(saved);   // the gateway now persists the new value too, as it would for real
    await act(async () => finish(saved));
    await waitFor(() => expect(tunnel).not.toBeDisabled());
    expect(autoSwitch).not.toBeDisabled();
    expect(tunnel).not.toBeChecked();
  });

  it("a failing save re-reads settings from the gateway instead of trusting its own rollback", async () => {
    const { api$ } = await openSubscriptions();
    const tunnel = await screen.findByRole("switch", { name: "Fetch subscriptions through the tunnel" });
    await waitFor(() => expect(tunnel).toBeChecked());
    const readsBefore = api$.getSettings.mock.calls.length;
    api$.putSettings.mockRejectedValue(new ApiError(422, "tunneled_fetch: invalid"));
    await userEvent.click(tunnel);
    await waitFor(() => expect(api$.getSettings.mock.calls.length).toBeGreaterThan(readsBefore));
    expect(tunnel).toBeChecked();
  });
});

describe("Subscriptions › fetch settings that re-apply the tunnel", () => {
  const STOPPED = { ...STATUS, running: false, xray_state: "stopped" };

  it("the tunneled fetch saves as a connection write, the auto-switch as a plain settings write", async () => {
    const { api$, client } = await openSubscriptions();
    const tunnel = await screen.findByRole("switch", { name: "Fetch subscriptions through the tunnel" });
    const autoSwitch = screen.getByRole("switch", { name: "Subscription auto-switch" });
    await waitFor(() => expect(tunnel).toBeChecked());
    let finish: (settings: Settings) => void = () => {};
    api$.putSettings.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    await userEvent.click(tunnel);
    await waitFor(() => expect(api$.putSettings).toHaveBeenCalledWith({ tunneled_fetch: false }));
    expect([isConnectionBusy(client), isSettingsBusy(client)]).toEqual([true, true]);
    await act(async () => finish({ ...SETTINGS, tunneled_fetch: false }));
    await waitFor(() => expect(autoSwitch).toBeEnabled());

    await userEvent.click(autoSwitch);
    expect(api$.putSettings).toHaveBeenLastCalledWith({ subs_auto_switch: false });
    expect([isConnectionBusy(client), isSettingsBusy(client)]).toEqual([false, true]);
    await act(async () => finish({ ...SETTINGS, tunneled_fetch: false, subs_auto_switch: false }));
  });

  it("while a connection write runs the tunneled fetch waits and the auto-switch does not", async () => {
    const { client } = await openSubscriptions();
    const tunnel = await screen.findByRole("switch", { name: "Fetch subscriptions through the tunnel" });
    await waitFor(() => expect(tunnel).toBeChecked());
    const release = holdConnectionWrite(client);
    await waitFor(() => expect(tunnel).toBeDisabled());
    expect(screen.getByRole("switch", { name: "Subscription auto-switch" })).toBeEnabled();
    await release();
    await waitFor(() => expect(tunnel).toBeEnabled());
  });

  it("a 502 says nothing was saved, puts the switch back and re-reads what a re-apply can move", async () => {
    const { api$ } = await openSubscriptions();
    const error = vi.spyOn(toast, "error");
    const tunnel = await screen.findByRole("switch", { name: "Fetch subscriptions through the tunnel" });
    await waitFor(() => expect(tunnel).toBeChecked());
    const reads = { settings: api$.getSettings.mock.calls.length, status: api$.getStatus.mock.calls.length };
    api$.putSettings.mockRejectedValue(new ApiError(502, "xray -test failed: bad outbound"));
    await userEvent.click(tunnel);
    await waitFor(() => expect(error).toHaveBeenCalledWith("not saved — applying to the tunnel failed: xray -test failed: bad outbound", { duration: 20000 }));
    expect(tunnel).toBeChecked();
    await waitFor(() => expect(api$.getSettings.mock.calls.length).toBeGreaterThan(reads.settings));
    expect(api$.getStatus.mock.calls.length).toBeGreaterThan(reads.status);
  });

  it("with xray stopped and a node still selected it asks before starting the tunnel; Cancel sends nothing", async () => {
    const { api$, client } = await openSubscriptions();
    api$.getStatus.mockResolvedValue(STOPPED);
    await act(() => client.refetchQueries({ queryKey: ["status"] }));
    const tunnel = await screen.findByRole("switch", { name: "Fetch subscriptions through the tunnel" });
    await waitFor(() => expect(tunnel).toBeChecked());
    await userEvent.click(tunnel);
    const dialog = await screen.findByRole("dialog", { name: "Confirm" });
    expect(dialog).toHaveTextContent("This starts the tunnel again. Continue?");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(api$.putSettings).not.toHaveBeenCalled();
    expect(tunnel).toBeChecked();

    await userEvent.click(tunnel);
    await userEvent.click(within(await screen.findByRole("dialog", { name: "Confirm" })).getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(api$.putSettings).toHaveBeenCalledWith({ tunneled_fetch: false }));
  });

  it("a connection write that started while the question was open: nothing is sent, and it says why", async () => {
    const { api$, client } = await openSubscriptions();
    api$.getStatus.mockResolvedValue(STOPPED);
    await act(() => client.refetchQueries({ queryKey: ["status"] }));
    const error = vi.spyOn(toast, "error");
    const tunnel = await screen.findByRole("switch", { name: "Fetch subscriptions through the tunnel" });
    await waitFor(() => expect(tunnel).toBeChecked());
    await userEvent.click(tunnel);
    const dialog = await screen.findByRole("dialog", { name: "Confirm" });
    const release = holdConnectionWrite(client);
    await userEvent.click(within(dialog).getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith(CONNECTION_BUSY, { duration: 20000 }));
    expect(api$.putSettings).not.toHaveBeenCalled();
    await release();
  });

  it("the auto-switch re-applies nothing, so it never asks", async () => {
    const { api$, client } = await openSubscriptions();
    api$.getStatus.mockResolvedValue(STOPPED);
    await act(() => client.refetchQueries({ queryKey: ["status"] }));
    const autoSwitch = await screen.findByRole("switch", { name: "Subscription auto-switch" });
    await waitFor(() => expect(autoSwitch).toBeChecked());
    await userEvent.click(autoSwitch);
    expect(screen.queryByRole("dialog", { name: "Confirm" })).toBeNull();
    expect(api$.putSettings).toHaveBeenCalledWith({ subs_auto_switch: false });
  });
});

describe("Subscriptions › actions (U2–U4, U6)", () => {
  it("Refresh is busy while it runs and reports the gateway's status", async () => {
    const { api$ } = await openSubscriptions();
    const success = vi.spyOn(toast, "success");
    let finish: (result: unknown) => void = () => {};
    api$.refreshSub.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    await userEvent.click(within(card("work")).getByRole("button", { name: "Refresh" }));
    expect(api$.refreshSub).toHaveBeenCalledWith(1);
    expect(within(card("work")).getByRole("button", { name: "Refreshing…" })).toBeDisabled();
    await act(async () => finish({ ok: true, status: "ok: +2 ~10 -0", error: null }));
    await waitFor(() => expect(success).toHaveBeenCalledWith("work: ok: +2 ~10 -0", { duration: 8000 }));
  });

  it("a refresh that could not fetch comes back ok: false and reads as an error", async () => {
    const { api$ } = await openSubscriptions();
    api$.refreshSub.mockResolvedValue({ ok: false, status: "error: fetch failed: timeout", error: "fetch failed: timeout" });
    const error = vi.spyOn(toast, "error");
    await userEvent.click(within(card("old")).getByRole("button", { name: "Refresh" }));   // a paused one still refreshes by hand
    await waitFor(() => expect(error).toHaveBeenCalledWith("old: error: fetch failed: timeout", { duration: 20000 }));
  });

  it("Refresh all keeps its result on screen until dismissed", async () => {
    const { api$, client } = await openSubscriptions();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await userEvent.click(screen.getByRole("button", { name: "Refresh all" }));
    const panel = await screen.findByRole("status", { name: "Refresh all result" });
    expect(panel).toHaveTextContent("1/2 refreshed · 1 failed — home: fetch failed: timeout");
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["status"] });
    api$.refreshAllSubs.mockResolvedValue({ attempted: 2, succeeded: 2, failed: 0, results: [] } satisfies RefreshAllResult);
    await userEvent.click(screen.getByRole("button", { name: "Refresh all" }));
    await waitFor(() => expect(screen.getByRole("status", { name: "Refresh all result" })).toHaveTextContent("2/2 refreshed"));
    await userEvent.click(screen.getByRole("button", { name: "Dismiss refresh result" }));
    expect(screen.queryByRole("status", { name: "Refresh all result" })).toBeNull();
  });

  it("one refresh at a time: Refresh all waits for a card's Refresh, and every card's Refresh waits for Refresh all", async () => {
    const { api$ } = await openSubscriptions();
    let finishOne: (result: RefreshResult) => void = () => {};
    api$.refreshSub.mockImplementation(() => new Promise((resolve) => { finishOne = resolve; }));
    await userEvent.click(within(card("work")).getByRole("button", { name: "Refresh" }));
    expect(screen.getByRole("button", { name: "Refresh all" })).toBeDisabled();
    expect(within(card("home")).getByRole("button", { name: "Refresh" })).toBeDisabled();
    await act(async () => finishOne({ ok: true, status: "ok: +0 ~6 -0", error: null }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Refresh all" })).toBeEnabled());

    let finishAll: (result: RefreshAllResult) => void = () => {};
    api$.refreshAllSubs.mockImplementation(() => new Promise((resolve) => { finishAll = resolve; }));
    await userEvent.click(screen.getByRole("button", { name: "Refresh all" }));
    for (const name of ["work", "home", "old"]) expect(within(card(name)).getByRole("button", { name: "Refresh" })).toBeDisabled();
    await act(async () => finishAll(REFRESH_ALL));
    await waitFor(() => expect(within(card("work")).getByRole("button", { name: "Refresh" })).toBeEnabled());
  });

  it("Refresh all is off when every subscription is paused", async () => {
    const api$ = mockApi();
    api$.listSubs.mockResolvedValue(SUBS.map((sub): Subscription => ({ ...sub, enabled: false })));
    renderApp("/nodes/subscriptions");
    await screen.findByRole("region", { name: "work" });
    expect(screen.getByRole("button", { name: "Refresh all" })).toBeDisabled();
  });

  it("Pause and Resume switch auto-update", async () => {
    const { api$ } = await openSubscriptions();
    const success = vi.spyOn(toast, "success");
    await userEvent.click(within(card("work")).getByRole("button", { name: "Pause" }));
    expect(api$.updateSub).toHaveBeenCalledWith(1, { enabled: false });
    await waitFor(() => expect(success).toHaveBeenCalledWith("Paused work", { duration: 8000 }));
    await userEvent.click(within(card("old")).getByRole("button", { name: "Resume" }));
    expect(api$.updateSub).toHaveBeenCalledWith(3, { enabled: true });
  });

  it("a failed Pause or Resume says which one failed", async () => {
    const { api$ } = await openSubscriptions();
    api$.updateSub.mockRejectedValue(new Error("offline"));
    const error = vi.spyOn(toast, "error");
    await userEvent.click(within(card("old")).getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("resume failed", { duration: 20000 }));
    await userEvent.click(within(card("work")).getByRole("button", { name: "Pause" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("pause failed", { duration: 20000 }));
  });

  it("Delete asks with the contract's words first", async () => {
    const { api$ } = await openSubscriptions();
    await userEvent.click(within(card("work")).getByRole("button", { name: "Delete" }));
    let dialog = await screen.findByRole("dialog", { name: "Confirm" });
    expect(dialog).toHaveTextContent('Delete subscription "work"? Its 6 node(s) are detached to Servers (an active connection is kept).');
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(api$.deleteSub).not.toHaveBeenCalled();
    await userEvent.click(within(card("work")).getByRole("button", { name: "Delete" }));
    dialog = await screen.findByRole("dialog", { name: "Confirm" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api$.deleteSub).toHaveBeenCalledWith(1));
  });
});
