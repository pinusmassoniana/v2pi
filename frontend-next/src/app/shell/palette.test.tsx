import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { api, type Status } from "../../api/client";
import { STATUS, mockApi } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";
import { closePalette, openPalette } from "./palette";

afterEach(() => act(() => closePalette()));

describe("command palette", () => {
  it("Ctrl/⌘+K opens it and a screen can be reached by name", async () => {
    mockApi();
    const { router } = renderApp("/");
    await screen.findByRole("heading", { level: 1, name: "Overview" });
    await userEvent.keyboard("{Control>}k{/Control}");
    await userEvent.type(await screen.findByPlaceholderText("Search nodes, screens and actions…"), "Anti-DPI");
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(router.state.location.pathname).toBe("/tunnel/anti-dpi"));
  });

  it("finds a node by name and opens its detail", async () => {
    mockApi();
    const { router } = renderApp("/");
    await userEvent.click(await screen.findByRole("button", { name: "Search and commands" }));
    await userEvent.type(await screen.findByPlaceholderText("Search nodes, screens and actions…"), "de-fra");
    // Home shows node names too: pick the palette's own option.
    await userEvent.click(await screen.findByRole("option", { name: /^de-fra-01/ }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/nodes/2"));
  });

  it("nodes mode connects the picked node and refreshes the connection state", async () => {
    mockApi();
    const apply = vi.spyOn(api, "apply").mockResolvedValue({ ok: true });
    const { client } = renderApp("/");
    const invalidateQueries = vi.spyOn(client, "invalidateQueries");
    await screen.findByRole("heading", { level: 1 });
    act(() => openPalette("nodes"));
    await userEvent.click(await screen.findByRole("option", { name: /^nl-ams-03/ }));
    await waitFor(() => expect(apply).toHaveBeenCalledWith(1));
    await waitFor(() => expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ["status"] }));
  });

  it("roll back asks first and does nothing when declined", async () => {
    mockApi();
    const rollback = vi.spyOn(api, "rollback").mockResolvedValue({ ok: true });
    renderApp("/");
    await screen.findByRole("heading", { level: 1 });

    act(() => openPalette());
    await userEvent.click(await screen.findByText("Roll back to previous node"));
    await userEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(rollback).not.toHaveBeenCalled();

    act(() => openPalette());
    await userEvent.click(await screen.findByText("Roll back to previous node"));
    const dialog = await screen.findByRole("dialog", { name: "Confirm" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Roll back" }));
    await waitFor(() => expect(rollback).toHaveBeenCalledTimes(1));
  });

  it("a connect from the palette is a connection write: the other connection controls wait for it", async () => {
    const api$ = mockApi();
    api$.getStatus.mockResolvedValue({ ...STATUS, prev_active_node_id: 2 });
    let finish: () => void = () => {};
    vi.spyOn(api, "connectBest").mockImplementation(() => new Promise((resolve) => { finish = () => resolve({ ok: true, node_id: 2 }); }));
    renderApp("/");
    const block = await screen.findByRole("region", { name: "Status" });
    const disconnect = await within(block).findByRole("button", { name: "Disconnect" });
    act(() => openPalette());
    await userEvent.click(await screen.findByText("Connect best"));
    await waitFor(() => expect(disconnect).toBeDisabled());
    expect(within(block).getByRole("button", { name: "Roll back to de-fra-01" })).toBeDisabled();
    act(() => openPalette());
    expect((await screen.findByText("Connect best")).closest("[cmdk-item]")).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByText("Roll back to previous node").closest("[cmdk-item]")).toHaveAttribute("aria-disabled", "true");
    act(() => closePalette());
    await act(async () => finish());
    await waitFor(() => expect(disconnect).toBeEnabled());
  });

  it("roll back is not offered when the gateway says it would not work", async () => {
    const api$ = mockApi();
    api$.getStatus.mockResolvedValue({ ...STATUS, prev_active_node_id: 2, rollback_available: false });
    renderApp("/");
    await screen.findByText("Tunnel online");
    act(() => openPalette());
    expect(await screen.findByText("Refresh all subscriptions")).toBeInTheDocument();
    expect(screen.queryByText("Roll back to previous node")).toBeNull();
  });

  it("roll back refuses when the target changed while the dialog was open", async () => {
    const api$ = mockApi();
    api$.getStatus.mockResolvedValue({ ...STATUS, prev_active_node_id: 2 });
    const rollback = vi.spyOn(api, "rollback").mockResolvedValue({ ok: true });
    const error = vi.spyOn(toast, "error");
    const { client } = renderApp("/");
    await screen.findByText("Tunnel online");
    act(() => openPalette());
    await userEvent.click(await screen.findByText("Roll back to previous node"));
    const dialog = await screen.findByRole("dialog", { name: "Confirm" });
    act(() => { client.setQueryData<Status>(["status"], (old) => ({ ...old!, prev_active_node_id: 3 })); });
    await userEvent.click(within(dialog).getByRole("button", { name: "Roll back" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("The rollback target changed — try again", { duration: 20000 }));
    expect(rollback).not.toHaveBeenCalled();
  });
});
