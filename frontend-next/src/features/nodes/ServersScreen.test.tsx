import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { ApiError, type Node } from "../../api/client";
import { NODES, NODE_HEALTH, NOW_SEC, SETTINGS, STATUS, SUBS, mockApi, mockNodeGroups } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";

afterEach(() => localStorage.clear());

/** The names of the rows on screen, in order (table rows and phone cards both carry data-node-id). */
const rowNames = () => [...document.querySelectorAll<HTMLElement>("[data-node-id]")].map((row) => row.dataset.nodeName);
const chips = () => within(screen.getByRole("navigation", { name: "Node groups" }));

async function openServers(path = "/nodes") {
  const api$ = mockNodeGroups(mockApi());
  const view = renderApp(path);
  await screen.findByRole("navigation", { name: "Node groups" });
  return { api$, ...view };
}

describe("Servers › groups (N1)", () => {
  it("shows the first subscription by default, with every group's count", async () => {
    await openServers();
    await waitFor(() => expect(rowNames()).toHaveLength(6));
    expect(chips().getAllByRole("link").map((link) => link.textContent)).toEqual(["work 6", "home 1", "old 0", "Servers 3"]);
    expect(chips().getByRole("link", { name: "work 6" })).toHaveAttribute("aria-current", "page");
    expect(rowNames()[0]).toBe("nl-ams-03");
  });

  it("a chip switches the group in the URL, keeping the search and sort", async () => {
    const { router } = await openServers("/nodes?sort=name");
    await waitFor(() => expect(rowNames()).toHaveLength(6));
    await userEvent.click(chips().getByRole("link", { name: "Servers 3" }));
    await waitFor(() => expect(router.state.location.search).toEqual({ group: "servers", sort: "name" }));
    await waitFor(() => expect(rowNames()).toEqual(["kz-ala-01", "lab-lan", "vps-hel"]));
    expect(chips().getByRole("link", { name: "Servers 3" })).toHaveAttribute("aria-current", "page");
  });

  it("an unknown group falls back to the default in place, without a new history entry", async () => {
    const { router } = await openServers("/nodes?group=9&q=example");
    const length = router.history.length;
    await waitFor(() => expect(router.state.location.search).toEqual({ q: "example" }));
    expect(router.history.length).toBe(length);
    await waitFor(() => expect(rowNames()).toHaveLength(6));
  });

  it("a group that disappears falls back to the default on the next refresh", async () => {
    const { api$, client, router } = await openServers("/nodes?group=2");
    await waitFor(() => expect(rowNames()).toHaveLength(1));
    api$.listSubs.mockResolvedValue(SUBS.filter((s) => s.id !== 2));
    await act(() => client.refetchQueries({ queryKey: ["subs"] }));
    await waitFor(() => expect(router.state.location.search).toEqual({}));
    expect(chips().getByRole("link", { name: "work 6" })).toHaveAttribute("aria-current", "page");
  });

  it("with no subscriptions the default is Servers", async () => {
    const api$ = mockNodeGroups(mockApi());
    api$.listSubs.mockResolvedValue([]);
    renderApp("/nodes");
    await waitFor(() => expect(rowNames()).toHaveLength(3));
    expect(chips().getAllByRole("link").map((link) => link.textContent)).toEqual(["Servers 3"]);
  });
});

describe("Servers › search, sort and density (N2–N4)", () => {
  it("search filters name, address and note, and lives in the URL", async () => {
    const { router } = await openServers();
    await waitFor(() => expect(rowNames()).toHaveLength(6));
    await userEvent.type(screen.getByRole("searchbox", { name: "Search nodes" }), "STREAM");
    await waitFor(() => expect(router.state.location.search).toEqual({ q: "STREAM" }));
    await waitFor(() => expect(rowNames()).toHaveLength(1));
    expect(rowNames()[0]).toBe("de-fra-01");
    await userEvent.clear(screen.getByRole("searchbox", { name: "Search nodes" }));
    await waitFor(() => expect(router.state.location.search).toEqual({}));
    await waitFor(() => expect(rowNames()).toHaveLength(6));
  });

  it("sort and direction write the URL, and the URL restores them", async () => {
    const { router } = await openServers();
    await waitFor(() => expect(rowNames()).toHaveLength(6));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Sort by" }), "tcp");
    await waitFor(() => expect(router.state.location.search).toEqual({ sort: "tcp" }));
    await userEvent.click(screen.getByRole("button", { name: "Sort ascending" }));
    await waitFor(() => expect(router.state.location.search).toEqual({ sort: "tcp", dir: "desc" }));
    await waitFor(() => expect(rowNames()[0]).toBe("ch-zrh-02"));
    expect(screen.getByRole("button", { name: "Sort descending" })).toBeInTheDocument();
  });

  it("opening a URL with search, sort and direction shows that list", async () => {
    await openServers("/nodes?group=1&q=example&sort=http&dir=asc");
    await waitFor(() => expect(rowNames()).toHaveLength(6));
    expect(rowNames()[0]).toBe("de-fra-01");
    expect(screen.getByRole("searchbox", { name: "Search nodes" })).toHaveValue("example");
    expect(screen.getByRole("combobox", { name: "Sort by" })).toHaveValue("http");
  });

  it("density is remembered across visits", async () => {
    const first = await openServers();
    const toggle = screen.getByRole("button", { name: "Compact rows" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(localStorage.getItem("nodes-density")).toBe("1");
    first.unmount();
    await openServers();
    expect(screen.getByRole("button", { name: "Compact rows" })).toHaveAttribute("aria-pressed", "true");
  });
});

describe("Servers › states (N19, N20)", () => {
  it("says Loading servers… while the list loads, never the empty message", async () => {
    const api$ = mockApi();
    api$.listNodes.mockReturnValue(new Promise<Node[]>(() => {}));
    renderApp("/nodes");
    expect(await screen.findByText("Loading servers…")).toBeInTheDocument();
    expect(screen.queryByText(/No servers here/)).toBeNull();
  });

  it("an empty group says so, and the Servers group says how to add one", async () => {
    const api$ = mockApi();
    renderApp("/nodes?group=3");
    expect(await screen.findByText("No servers here")).toBeInTheDocument();
    api$.listNodes.mockResolvedValue([]);
    await userEvent.click(chips().getByRole("link", { name: "Servers 0" }));
    expect(await screen.findByText("No servers here — add one with Add server.")).toBeInTheDocument();
  });

  it("a list that failed to load is an error with Retry; a failed refresh keeps the rows with a notice", async () => {
    const api$ = mockApi();
    api$.listNodes.mockRejectedValue(new ApiError(500, "boom"));
    const { client } = renderApp("/nodes");
    const alert = await screen.findByRole("alert", {}, { timeout: 3000 });
    expect(alert).toHaveTextContent("Servers did not load");
    api$.listNodes.mockResolvedValue(NODES);
    await userEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(rowNames()).toHaveLength(6));
    api$.listNodes.mockRejectedValue(new ApiError(500, "boom"));
    await act(() => client.refetchQueries({ queryKey: ["nodes"] }).catch(() => undefined));
    expect(await screen.findByText("Servers did not refresh — showing the last data")).toBeInTheDocument();
    expect(rowNames()).toHaveLength(6);
  });

  it("node health that failed to load says so with Retry, while the rows still show; Retry loads it", async () => {
    const api$ = mockApi();
    api$.listNodeHealth.mockRejectedValue(new ApiError(500, "boom"));
    renderApp("/nodes");
    const notice = await screen.findByText("Node health did not load", {}, { timeout: 3000 });
    expect(rowNames()).toHaveLength(6);
    expect(screen.queryByText("Servers did not load")).toBeNull();
    api$.listNodeHealth.mockResolvedValue(NODE_HEALTH);
    await userEvent.click(within(notice.closest<HTMLElement>("[role=alert]")!).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.queryByText("Node health did not load")).toBeNull());
    expect(document.querySelector('[data-node-name="de-fra-01"]')).toHaveTextContent("40 ms");
  });

  it("N20: auto-failover armed with the last switch, off with a past switch, nothing when neither — from the status poll", async () => {
    const api$ = mockApi();
    api$.getSettings.mockResolvedValue({ ...SETTINGS, failover_enabled: false });   // loaded once elsewhere; not what the note reads
    api$.getStatus.mockResolvedValue({ ...STATUS, last_failover_at: NOW_SEC - 720 });
    const { client } = renderApp("/nodes");
    const note = await screen.findByText(/Auto-failover/);
    expect(note).toHaveTextContent("⇄ Auto-failover armed · last switch 12m ago");

    api$.getStatus.mockResolvedValue({ ...STATUS, failover_enabled: false, last_failover_at: NOW_SEC - 720 });
    await act(() => client.refetchQueries({ queryKey: ["status"] }));
    await waitFor(() => expect(screen.getByText(/Auto-failover/)).toHaveTextContent("Auto-failover off · last switch 12m ago"));

    api$.getStatus.mockResolvedValue({ ...STATUS, failover_enabled: false });
    await act(() => client.refetchQueries({ queryKey: ["status"] }));
    await waitFor(() => expect(screen.queryByText(/Auto-failover/)).toBeNull());
    expect(rowNames()).toHaveLength(6);
    expect(api$.getSettings).not.toHaveBeenCalled();
  });
});
