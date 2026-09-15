import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type NodeHealth } from "../../api/client";
import { settleConfirm } from "../../components/confirm";
import { ALL_NODE_HEALTH, NODE_HEALTH, holdConnectionWrite, mockApi, mockNodeGroups } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";
import { setViewportWidth } from "../../test/viewport";
import { Elapsed } from "./Elapsed";

afterEach(() => {
  act(() => settleConfirm(false));
  vi.useRealTimers();
});

const toolbar = () => within(screen.getByRole("region", { name: "Servers toolbar" }));
const row = (name: string) => document.querySelector<HTMLElement>(`[data-node-name="${name}"]`)!;
const selectionBar = () => screen.getByRole("region", { name: "Selection" });
/** How many times `key` was invalidated through this spy. */
const invalidations = (spy: { mock: { calls: unknown[][] } }, key: string) =>
  spy.mock.calls.filter(([filters]) => JSON.stringify((filters as { queryKey?: unknown } | undefined)?.queryKey) === JSON.stringify([key])).length;

async function openList(path = "/nodes") {
  const api$ = mockNodeGroups(mockApi());
  const view = renderApp(path);
  await waitFor(() => expect(document.querySelectorAll("[data-node-id]").length).toBeGreaterThan(0));
  return { api$, ...view };
}

describe("Elapsed", () => {
  it("counts whole seconds on its own clock", () => {
    vi.useFakeTimers();
    render(<Elapsed since={Date.now()} />);
    expect(screen.getByText("0 s")).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(42_000); });
    expect(screen.getByText("42 s")).toBeInTheDocument();
  });
});

describe("Servers › group actions (N8–N10)", () => {
  it("TCP ping sweeps the group; both pings wait for it; its answer replaces the health list", async () => {
    const { api$ } = await openList();
    let finish: (rows: NodeHealth[]) => void = () => {};
    api$.probeTcp.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    await userEvent.click(toolbar().getByRole("button", { name: "TCP ping" }));
    expect(api$.probeTcp).toHaveBeenCalledWith("1");
    expect(toolbar().getByRole("button", { name: /^Pinging… \d+ s$/ })).toBeDisabled();
    expect(toolbar().getByRole("button", { name: "HTTP ping" })).toBeDisabled();
    await act(async () => finish(ALL_NODE_HEALTH.map((h) => ({ ...h, last_tcp_ms: 7 }))));
    await waitFor(() => expect(toolbar().getByRole("button", { name: "TCP ping" })).toBeEnabled());
    expect(within(row("de-fra-01")).getAllByText("7 ms")).toHaveLength(1);
  });

  it("HTTP ping of the Servers group, and a failed sweep says so", async () => {
    const { api$ } = await openList("/nodes?group=servers");
    api$.probeHttp.mockRejectedValue(new ApiError(0, "request timed out"));
    const error = vi.spyOn(toast, "error");
    await userEvent.click(toolbar().getByRole("button", { name: "HTTP ping" }));
    expect(api$.probeHttp).toHaveBeenCalledWith("servers");
    await waitFor(() => expect(error).toHaveBeenCalledWith("request timed out", { duration: 20000 }));
  });

  it("the ping lock survives the screen remounting mid-sweep", async () => {
    const { api$, client, unmount } = await openList();
    let finish: (rows: NodeHealth[]) => void = () => {};
    api$.probeTcp.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    await userEvent.click(toolbar().getByRole("button", { name: "TCP ping" }));
    unmount();
    renderApp("/nodes", { client });   // same client: a real remount, not a fresh app with an empty mutation cache
    await waitFor(() => expect(document.querySelectorAll("[data-node-id]").length).toBeGreaterThan(0));
    expect(toolbar().getByRole("button", { name: /^Pinging… \d+ s$/ })).toBeDisabled();
    expect(toolbar().getByRole("button", { name: "HTTP ping" })).toBeDisabled();
    await act(async () => finish(ALL_NODE_HEALTH));
    await waitFor(() => expect(toolbar().getByRole("button", { name: "TCP ping" })).toBeEnabled());
  });

  it("Test all probes every shown row in order, counting down, past a failure; health is re-read once at the end", async () => {
    const { api$, client } = await openList("/nodes?q=example&sort=name");
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const pending: ((health: NodeHealth) => void)[] = [];
    api$.probeNode.mockImplementation((id) => (id === 2
      ? Promise.reject(new Error("probe failed"))
      : new Promise((resolve) => { pending.push(resolve); })));
    await userEvent.click(toolbar().getByRole("button", { name: "Test all (real)" }));
    expect(toolbar().getByRole("button", { name: "Testing 6…" })).toBeDisabled();
    expect(api$.probeNode.mock.calls.map(([id]) => id)).toEqual([5]);            // ch-zrh-02 first by name
    await act(async () => pending.shift()!({ ...NODE_HEALTH[0]!, node_id: 5 }));
    await waitFor(() => expect(api$.probeNode.mock.calls.map(([id]) => id)).toEqual([5, 2, 3]));   // de-fra-01 failed, fi-hel-02 next
    expect(toolbar().getByRole("button", { name: "Testing 4…" })).toBeInTheDocument();
    for (let i = 0; i < 4; i++) await act(async () => pending.shift()!(NODE_HEALTH[0]!));
    await waitFor(() => expect(toolbar().getByRole("button", { name: "Test all (real)" })).toBeEnabled());
    expect(api$.probeNode.mock.calls.map(([id]) => id)).toEqual([5, 2, 3, 1, 4, 6]);
    expect(invalidations(invalidate, "nodeHealth")).toBe(1);
  });

  it("Test all counts thrown probe failures and reports them once, at the end", async () => {
    const { api$ } = await openList("/nodes?q=02");   // fi-hel-02 and ch-zrh-02 only, in position order
    const error = vi.spyOn(toast, "error");
    const pending: ((health: NodeHealth) => void)[] = [];
    api$.probeNode.mockImplementation((id) => (id === 3 ? Promise.reject(new Error("probe failed")) : new Promise((resolve) => { pending.push(resolve); })));
    await userEvent.click(toolbar().getByRole("button", { name: "Test all (real)" }));
    await waitFor(() => expect(api$.probeNode.mock.calls.map(([id]) => id)).toEqual([3, 5]));
    await act(async () => pending.shift()!(NODE_HEALTH[0]!));
    await waitFor(() => expect(toolbar().getByRole("button", { name: "Test all (real)" })).toBeEnabled());
    expect(error).toHaveBeenCalledWith("Test all: 1 of 2 probes failed — probe failed", { duration: 20000 });
  });

  it("Test all stops scheduling when the screen goes away", async () => {
    const { api$, unmount } = await openList();
    let finish: (health: NodeHealth) => void = () => {};
    api$.probeNode.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    await userEvent.click(toolbar().getByRole("button", { name: "Test all (real)" }));
    expect(api$.probeNode).toHaveBeenCalledTimes(1);
    unmount();
    await act(async () => finish(NODE_HEALTH[0]!));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(api$.probeNode).toHaveBeenCalledTimes(1);
  });

  it("Connect best connects the group's healthiest node and names it", async () => {
    const { api$ } = await openList();
    const success = vi.spyOn(toast, "success");
    await userEvent.click(toolbar().getByRole("button", { name: "Connect best" }));
    expect(api$.connectBest).toHaveBeenCalledWith(1);
    await waitFor(() => expect(success).toHaveBeenCalledWith("Connected to de-fra-01", { duration: 8000 }));
  });

  it("Connect best in Servers asks for the manual nodes; none connectable is a 404 with its own words", async () => {
    const { api$ } = await openList("/nodes?group=servers");
    api$.connectBest.mockRejectedValue(new ApiError(404, "no connectable node in this group"));
    const error = vi.spyOn(toast, "error");
    await userEvent.click(toolbar().getByRole("button", { name: "Connect best" }));
    expect(api$.connectBest).toHaveBeenCalledWith(null);
    await waitFor(() => expect(error).toHaveBeenCalledWith("No connectable node in this group", { duration: 20000 }));
  });

  it("Connect best waits for any other connection write", async () => {
    const { client } = await openList();
    const release = holdConnectionWrite(client);
    await waitFor(() => expect(toolbar().getByRole("button", { name: "Connect best" })).toBeDisabled());
    await release();
    await waitFor(() => expect(toolbar().getByRole("button", { name: "Connect best" })).toBeEnabled());
  });

  it("an empty group has nothing to ping, test or connect", async () => {
    mockNodeGroups(mockApi());
    renderApp("/nodes?group=3");
    await screen.findByText("No servers here");
    for (const name of ["TCP ping", "HTTP ping", "Test all (real)", "Connect best"]) expect(toolbar().getByRole("button", { name })).toBeDisabled();
  });
});

describe("Servers › selection and bulk (N18, T6)", () => {
  it("select all is tri-state over the shown rows; switching group clears the selection", async () => {
    await openList();
    const all = screen.getByRole("checkbox", { name: "Select all shown" });
    // The live region is there, empty, before the first tick — so the first count is announced too.
    const live = document.querySelector<HTMLElement>("[data-selection-count]")!;
    expect(live).toHaveAttribute("aria-live", "polite");
    expect(live).toBeEmptyDOMElement();
    await userEvent.click(within(row("de-fra-01")).getByRole("checkbox", { name: "Select de-fra-01" }));
    expect(all).toBePartiallyChecked();
    expect(document.querySelector("[data-selection-count]")).toBe(live);
    expect(live).toHaveTextContent("1 selected");
    expect(within(selectionBar()).getByText("1 selected")).toBeInTheDocument();
    await userEvent.click(all);
    expect(all).toBeChecked();
    expect(within(selectionBar()).getByText("6 selected")).toBeInTheDocument();
    await userEvent.click(all);
    expect(all).not.toBeChecked();
    expect(screen.queryByRole("region", { name: "Selection" })).toBeNull();

    await userEvent.click(within(row("de-fra-01")).getByRole("checkbox", { name: "Select de-fra-01" }));
    await userEvent.click(within(screen.getByRole("navigation", { name: "Node groups" })).getByRole("link", { name: "home 1" }));
    await waitFor(() => expect(row("us-nyc-01")).not.toBeNull());
    expect(screen.queryByRole("region", { name: "Selection" })).toBeNull();

    // N1: A → B → A must not bring A's old ticks back — the group changed twice, so nothing was ever re-selected.
    await userEvent.click(within(screen.getByRole("navigation", { name: "Node groups" })).getByRole("link", { name: "work 6" }));
    await waitFor(() => expect(row("de-fra-01")).not.toBeNull());
    expect(screen.queryByRole("region", { name: "Selection" })).toBeNull();
    expect(within(row("de-fra-01")).getByRole("checkbox", { name: "Select de-fra-01" })).not.toBeChecked();
  });

  it("the selection counts only rows still shown", async () => {
    await openList();
    await userEvent.click(within(row("de-fra-01")).getByRole("checkbox", { name: "Select de-fra-01" }));
    await userEvent.click(within(row("fi-hel-02")).getByRole("checkbox", { name: "Select fi-hel-02" }));
    expect(within(selectionBar()).getByText("2 selected")).toBeInTheDocument();
    await userEvent.type(screen.getByRole("searchbox", { name: "Search nodes" }), "hel");
    await waitFor(() => expect(within(selectionBar()).getByText("1 selected")).toBeInTheDocument());
  });

  it("assigns a tuning profile node by node, then clears the selection; the lists are re-read once", async () => {
    const { api$, client } = await openList();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const success = vi.spyOn(toast, "success");
    await userEvent.click(within(row("de-fra-01")).getByRole("checkbox", { name: "Select de-fra-01" }));
    await userEvent.click(within(row("fi-hel-02")).getByRole("checkbox", { name: "Select fi-hel-02" }));
    const select = within(selectionBar()).getByRole("combobox", { name: "Assign tuning profile" });
    await waitFor(() => expect(within(select).getByRole("option", { name: "fragment-tls" })).toBeInTheDocument());
    expect(within(select).getAllByRole("option").map((option) => option.textContent)).toEqual(["Assign profile…", "(global default)", "balanced", "fragment-tls"]);
    await userEvent.selectOptions(select, "fragment-tls");
    await waitFor(() => expect(success).toHaveBeenCalledWith("Assigned fragment-tls to 2 node(s)", { duration: 8000 }));
    expect(api$.updateNode.mock.calls).toEqual([[2, { tuning_profile_id: 2 }], [3, { tuning_profile_id: 2 }]]);
    expect(screen.queryByRole("region", { name: "Selection" })).toBeNull();
    expect(invalidations(invalidate, "nodes")).toBe(1);
  });

  it("(global default) clears the profile; the first failure stops the run and names its node", async () => {
    const { api$ } = await openList();
    api$.updateNode.mockRejectedValueOnce(new ApiError(409, "disconnect the active node before editing it"));
    const error = vi.spyOn(toast, "error");
    await userEvent.click(within(row("nl-ams-03")).getByRole("checkbox", { name: "Select nl-ams-03" }));
    await userEvent.click(within(row("de-fra-01")).getByRole("checkbox", { name: "Select de-fra-01" }));
    await userEvent.selectOptions(within(selectionBar()).getByRole("combobox", { name: "Assign tuning profile" }), "(global default)");
    await waitFor(() => expect(error).toHaveBeenCalledWith("nl-ams-03: That node is active. Disconnect → Edit → Connect, then try again.", { duration: 20000 }));
    expect(api$.updateNode.mock.calls).toEqual([[1, { tuning_profile_id: null }]]);
    expect(within(selectionBar()).getByText("2 selected")).toBeInTheDocument();
  });

  it("a subscription's nodes are detached to Servers in one call; there is no bulk delete there", async () => {
    const { api$ } = await openList();
    const success = vi.spyOn(toast, "success");
    await userEvent.click(within(row("de-fra-01")).getByRole("checkbox", { name: "Select de-fra-01" }));
    await userEvent.click(within(row("se-sto-01")).getByRole("checkbox", { name: "Select se-sto-01" }));
    expect(within(selectionBar()).queryByRole("button", { name: "Delete" })).toBeNull();
    await userEvent.click(within(selectionBar()).getByRole("button", { name: "Detach to Servers" }));
    expect(api$.detachNodes).toHaveBeenCalledWith([2, 6]);
    await waitFor(() => expect(success).toHaveBeenCalledWith("Detached 2 node(s) to Servers", { duration: 8000 }));
  });

  it("Servers: bulk delete asks first, deletes in turn and stops at the first failure, then re-reads the lists once", async () => {
    const { api$, client } = await openList("/nodes?group=servers");
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const error = vi.spyOn(toast, "error");
    expect(screen.queryByRole("button", { name: "Detach to Servers" })).toBeNull();
    for (const name of ["vps-hel", "lab-lan", "kz-ala-01"]) await userEvent.click(within(row(name)).getByRole("checkbox", { name: `Select ${name}` }));
    await userEvent.click(within(selectionBar()).getByRole("button", { name: "Delete" }));
    let dialog = await screen.findByRole("dialog", { name: "Confirm" });
    expect(dialog).toHaveTextContent("Delete 3 server(s)?");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(api$.deleteNode).not.toHaveBeenCalled();

    api$.deleteNode.mockResolvedValueOnce({ ok: true }).mockRejectedValueOnce(new ApiError(500, "database is locked"));
    await userEvent.click(within(selectionBar()).getByRole("button", { name: "Delete" }));
    dialog = await screen.findByRole("dialog", { name: "Confirm" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("lab-lan: database is locked", { duration: 20000 }));
    expect(api$.deleteNode.mock.calls).toEqual([[7], [8]]);
    expect(invalidations(invalidate, "nodes")).toBe(1);
  });

  it("Clear empties the selection", async () => {
    await openList();
    await userEvent.click(within(row("de-fra-01")).getByRole("checkbox", { name: "Select de-fra-01" }));
    await userEvent.click(within(selectionBar()).getByRole("button", { name: "Clear" }));
    expect(screen.queryByRole("region", { name: "Selection" })).toBeNull();
    expect(within(row("de-fra-01")).getByRole("checkbox", { name: "Select de-fra-01" })).not.toBeChecked();
  });

  it("phone: Select turns the cards' buttons into checkboxes; Done leaves select mode and clears it", async () => {
    setViewportWidth(390);
    await openList();
    expect(screen.queryByRole("checkbox")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Select" }));
    expect(within(row("de-fra-01")).queryByRole("button", { name: "Connect de-fra-01" })).toBeNull();
    await userEvent.click(within(row("de-fra-01")).getByRole("checkbox", { name: "Select de-fra-01" }));
    expect(within(selectionBar()).getByText("1 selected")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Select all shown" })).toBePartiallyChecked();
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.queryByRole("region", { name: "Selection" })).toBeNull();
    expect(within(row("de-fra-01")).getByRole("button", { name: "Connect de-fra-01" })).toBeInTheDocument();
  });
});
