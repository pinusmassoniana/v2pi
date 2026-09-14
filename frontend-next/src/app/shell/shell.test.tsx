import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { describe, expect, it, vi } from "vitest";
import { ApiError, type Status } from "../../api/client";
import { STATUS, mockApi } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";
import { SECTIONS } from "../nav";
import { ErrorBoundary } from "./ErrorBoundary";

function Boom(): never {
  throw new Error("boom");
}

describe("shell", () => {
  it("desktop sidebar lists all 13 tabs in section order and marks the current one", async () => {
    mockApi();
    renderApp("/tunnel/health");
    const nav = await screen.findByRole("navigation", { name: "Primary" });
    expect(within(nav).getAllByRole("link").map((link) => link.textContent))
      .toEqual(SECTIONS.flatMap((s) => s.tabs.map((t) => t.label)));
    expect(within(nav).getByRole("link", { name: "Health & failover" })).toHaveAttribute("aria-current", "page");
  });

  it("phone: five sections below, the section's tabs on top; a node detail belongs to Servers", async () => {
    mockApi();
    renderApp("/nodes/12");
    const sections = await screen.findByRole("navigation", { name: "Sections" });
    expect(within(sections).getAllByRole("link").map((link) => link.textContent))
      .toEqual(["Home", "Nodes", "Tunnel", "Gateway", "System"]);
    expect(within(sections).getByRole("link", { name: "Nodes" })).toHaveAttribute("aria-current", "page");
    const tabs = screen.getByRole("navigation", { name: "Nodes tabs" });
    expect(within(tabs).getByRole("link", { name: "Servers" })).toHaveAttribute("aria-current", "page");
  });

  it("topbar: the title follows the route and the tunnel state is spelled out", async () => {
    const api$ = mockApi();
    renderApp("/gateway/remote-access");
    expect(await screen.findByRole("heading", { level: 1, name: "Remote access" })).toHaveClass("page-title");
    expect(await screen.findByText("Tunnel online")).toHaveClass("text-ok");
    expect(api$.connectTraffic).not.toHaveBeenCalled();   // the pill never opens the traffic socket itself
  });

  it("topbar: offline with no active node or with xray stopped, unknown while health is not fresh", async () => {
    const api$ = mockApi();
    api$.getStatus.mockResolvedValue({ ...STATUS, active_node_id: null, tunnel_online: false });
    const { client } = renderApp("/nodes");
    expect(await screen.findByText("Tunnel offline")).toHaveClass("text-bad");
    api$.getStatus.mockResolvedValue({ ...STATUS, running: false, tunnel_online: false });
    await act(() => client.refetchQueries({ queryKey: ["status"] }));
    expect(screen.getByText("Tunnel offline")).toHaveClass("text-bad");
    api$.getStatus.mockResolvedValue({ ...STATUS, tunnel_online: false, active_health_fresh: false });
    await act(() => client.refetchQueries({ queryKey: ["status"] }));
    expect(await screen.findByText("Tunnel unknown")).toHaveClass("text-t2");
  });

  it("xray-core toggle stops the engine and refreshes the connection state", async () => {
    const api$ = mockApi();
    const { client } = renderApp("/");
    const invalidateQueries = vi.spyOn(client, "invalidateQueries");
    const [toggle] = await screen.findAllByRole("switch", { name: "xray-core" });
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "true"));
    await userEvent.click(toggle!);
    await waitFor(() => expect(api$.xrayStop).toHaveBeenCalled());
    await waitFor(() => expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ["status"] }));
  });

  it("xray-core toggle shows the new state at once and stays disabled until the status refetch lands", async () => {
    const api$ = mockApi();
    renderApp("/nodes");   // one card: the sidebar's
    const toggle = await screen.findByRole("switch", { name: "xray-core" });
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "true"));
    let land: (status: Status) => void = () => {};
    api$.getStatus.mockImplementation(() => new Promise<Status>((resolve) => { land = resolve; }));

    await userEvent.click(toggle);
    await waitFor(() => expect(api$.xrayStop).toHaveBeenCalled());
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "false"));
    expect(toggle).toBeDisabled();

    await act(async () => land({ ...STATUS, running: false, xray_state: "stopped" }));
    await waitFor(() => expect(toggle).toBeEnabled());
    expect(toggle).toHaveAttribute("aria-checked", "false");
  });

  it("a failed xray-core toggle keeps the old state and says why", async () => {
    const api$ = mockApi();
    api$.xrayStop.mockRejectedValue(new ApiError(500, "xray did not stop"));
    const error = vi.spyOn(toast, "error");
    renderApp("/nodes");
    const toggle = await screen.findByRole("switch", { name: "xray-core" });
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "true"));
    await userEvent.click(toggle);
    await waitFor(() => expect(error).toHaveBeenCalledWith("xray did not stop", { duration: 20000 }));
    expect(toggle).toHaveAttribute("aria-checked", "true");
    expect(toggle).toBeEnabled();
  });

  it("xray-core reads the engine the same way as Home's xray chip", async () => {
    const api$ = mockApi();
    api$.getStatus.mockResolvedValue({ ...STATUS, running: true, xray_state: "error" });
    const { client } = renderApp("/nodes");
    const toggle = await screen.findByRole("switch", { name: "xray-core" });
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "true"));   // running wins, as the chip's RUNNING
    expect(screen.getByText("RUNNING")).toHaveClass("text-ok");
    api$.getStatus.mockResolvedValue({ ...STATUS, running: false, xray_state: "error" });
    await act(() => client.refetchQueries({ queryKey: ["status"] }));
    expect(await screen.findByText("RECONNECTING")).toHaveClass("text-warn");
    expect(toggle).toHaveAttribute("aria-checked", "false");
    api$.getStatus.mockResolvedValue({ ...STATUS, running: false, xray_state: "stopped" });
    await act(() => client.refetchQueries({ queryKey: ["status"] }));
    expect(await screen.findByText("STOPPED")).toHaveClass("text-bad");
  });

  it("theme toggle flips the document theme", async () => {
    mockApi();
    document.documentElement.dataset.theme = "dark";
    renderApp("/");
    await userEvent.click(await screen.findByRole("button", { name: "Toggle theme" }));
    expect(document.documentElement.dataset.theme).toBe("light");
  });

  it("says the panel is unreachable when the status poll fails, without blanking the screen", async () => {
    const api$ = mockApi();
    const { client } = renderApp("/");
    await screen.findByText("Tunnel online");
    api$.getStatus.mockRejectedValue(new ApiError(0, "network error"));
    await act(() => client.refetchQueries({ queryKey: ["status"] }));
    expect(await screen.findByText(/Can't reach the panel — showing data from/)).toBeInTheDocument();
    expect(screen.getByText("Tunnel unknown")).toHaveClass("text-t2");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Overview");
  });

  it("log out sits in the topbar on desktop and in the System section on a phone", async () => {
    mockApi();
    const { logout } = renderApp("/system/panel");
    const buttons = await screen.findAllByRole("button", { name: "Log out" });
    expect(buttons).toHaveLength(2);
    await userEvent.click(buttons[0]!);
    expect(logout).toHaveBeenCalled();
  });
});

describe("ErrorBoundary", () => {
  it("shows a reload card instead of taking the shell down, and resets when the route changes", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const { rerender } = render(<ErrorBoundary resetKey="/a"><Boom /></ErrorBoundary>);
    expect(screen.getByRole("alert")).toHaveTextContent("This screen crashed");
    expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();
    rerender(<ErrorBoundary resetKey="/b"><p>fine</p></ErrorBoundary>);
    expect(screen.getByText("fine")).toBeInTheDocument();
  });
});
