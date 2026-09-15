import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type RefreshAllResult, type Settings, type Subscription } from "../../api/client";
import { settleConfirm } from "../../components/confirm";
import { NOW_SEC, SETTINGS, SUBS, mockApi } from "../../test/fixtures";
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
