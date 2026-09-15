import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type NodeHealth, type Status } from "../../api/client";
import { keys } from "../../api/keys";
import { settleConfirm } from "../../components/confirm";
import {
  ALL_NODES, ALL_NODE_HEALTH, NODE_HEALTH, NOW_SEC, STATUS, TRAFFIC_FRAME, holdConnectionWrite, mockApi, mockNodeGroups, node,
} from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";
import { setViewportWidth } from "../../test/viewport";
import { checkedAgo } from "./list";

// Pass-through spy: every row calls checkedAgo when it renders, so its call count shows how many rows re-render.
vi.mock("./list", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./list")>();
  return { ...actual, checkedAgo: vi.fn(actual.checkedAgo) };
});

afterEach(() => act(() => settleConfirm(false)));
afterEach(() => vi.useRealTimers());

const table = () => screen.getByRole("table", { name: "Nodes" });
const row = (name: string) => table().querySelector<HTMLElement>(`tr[data-node-name="${name}"]`)!;
const rowNames = () => [...document.querySelectorAll<HTMLElement>("[data-node-id]")].map((el) => el.dataset.nodeName);

/** On fake timers Testing Library's waitFor never polls: step the clock until `check` holds. */
async function stepUntil(check: () => unknown) {
  for (let i = 0; i < 80 && !check(); i++) await act(() => vi.advanceTimersByTimeAsync(25));
  expect(check()).toBeTruthy();
}

async function openList(path = "/nodes", status: Partial<Status> = {}) {
  const api$ = mockNodeGroups(mockApi());
  api$.getStatus.mockResolvedValue({ ...STATUS, ...status });
  const view = renderApp(path);
  await waitFor(() => expect(document.querySelectorAll("[data-node-id]").length).toBeGreaterThan(0));
  return { api$, ...view };
}

describe("Servers › table (N5)", () => {
  it("is a real table whose sorted column carries aria-sort, sortable from its headers", async () => {
    const { router } = await openList();
    expect(within(table()).getAllByRole("columnheader").map((th) => th.textContent)).toEqual([
      "Name", "Address", "Port", "Transport", "TCP", "HTTP", "Real", "Egress", "Trend", "Checked", "Actions",
    ]);
    expect(table().querySelector("[aria-sort]")).toBeNull();   // position order sorts by nothing shown
    await userEvent.click(within(table()).getByRole("button", { name: "Name" }));
    await waitFor(() => expect(router.state.location.search).toEqual({ sort: "name" }));
    expect(within(table()).getByRole("columnheader", { name: /Name/ })).toHaveAttribute("aria-sort", "ascending");
    await userEvent.click(within(table()).getByRole("button", { name: /Name/ }));
    await waitFor(() => expect(router.state.location.search).toEqual({ sort: "name", dir: "desc" }));
    expect(within(table()).getByRole("columnheader", { name: /Name/ })).toHaveAttribute("aria-sort", "descending");
    expect(rowNames()[0]).toBe("se-sto-01");
    await userEvent.click(within(table()).getByRole("button", { name: "TCP" }));
    await waitFor(() => expect(router.state.location.search).toEqual({ sort: "tcp" }));
  });

  it("the active row: connected with its uptime, the failure counter, pills, egress, trend and age", async () => {
    const api$ = mockNodeGroups(mockApi());
    api$.listNodeHealth.mockResolvedValue(ALL_NODE_HEALTH.map((h) => (h.node_id === 1 ? { ...h, fail_count: 2 } : h)));
    renderApp("/nodes");
    await waitFor(() => expect(row("nl-ams-03")).not.toBeNull());
    const active = row("nl-ams-03");
    await waitFor(() => expect(active).toHaveTextContent("connected · 1m"));
    expect(active).toHaveAttribute("data-active", "true");
    expect(within(active).getByText("fail 2")).toHaveAttribute("title", "Consecutive real-request failures (auto-failover counter)");
    const cells = [...active.querySelectorAll("td")].map((td) => td.textContent);
    expect(cells.slice(1, 8)).toEqual(["nl-ams-03.example.org", "443", "vision · reality", "31 ms", "142 ms", "45 ms", "🇳🇱 185.107.56.21"]);
    expect(cells[9]).toBe("5 s ago");
    expect(active.querySelectorAll("td")[8]!.querySelector("svg path")).not.toBeNull();
    expect(active).toHaveTextContent("#1 · 443 · vision · reality");
  });

  it("the other rows: no counter off the active node, slow, failed and never probed pills, notes", async () => {
    await openList();
    expect(within(row("se-sto-01")).queryByText(/^fail \d/)).toBeNull();   // fail_count 3, but not the active node
    expect(within(row("se-sto-01")).getByText("failed")).toHaveAttribute("data-state", "failed");
    expect(within(row("pl-waw-01")).getAllByText("164 ms")[0]).toHaveAttribute("data-state", "slow");
    expect(within(row("pl-waw-01")).getByText("xhttp · tls")).toBeInTheDocument();
    expect(within(row("ch-zrh-02")).getAllByText("—").length).toBeGreaterThanOrEqual(3);
    expect(row("de-fra-01")).toHaveTextContent("streaming backup");
    expect(within(row("de-fra-01")).getByRole("link", { name: "de-fra-01" })).toHaveAttribute("href", "/nodes/2");
  });

  it("a stale node is dimmed with its badge", async () => {
    await openList("/nodes?group=2");
    await waitFor(() => expect(row("us-nyc-01")).not.toBeNull());
    expect(row("us-nyc-01")).toHaveAttribute("data-stale", "true");
    expect(row("us-nyc-01")).toHaveClass("opacity-55");
    expect(within(row("us-nyc-01")).getByText("stale")).toHaveAttribute("title", "Vanished from its subscription; kept because it was active or for history");
  });

  it("a detail link keeps the list's search and sort", async () => {
    await openList("/nodes?q=example&sort=name");
    await waitFor(() => expect(row("de-fra-01")).not.toBeNull());
    expect(within(row("de-fra-01")).getByRole("link", { name: "de-fra-01" })).toHaveAttribute("href", "/nodes/2?q=example&sort=name");
  });

  it("a live frame updates the active node's real pill without re-rendering the rows", async () => {
    const { api$ } = await openList();
    api$.emitTraffic(TRAFFIC_FRAME);
    const real = () => row("nl-ams-03").querySelectorAll("td")[6]!.firstElementChild!;
    await waitFor(() => expect(real()).toHaveAttribute("data-live", "true"));
    expect(real()).toHaveTextContent("42 ms");
    const renders = vi.mocked(checkedAgo).mock.calls.length;
    for (let i = 1; i <= 5; i++) api$.emitTraffic({ ...TRAFFIC_FRAME, ts: TRAFFIC_FRAME.ts + i * 1_000, active: { ...TRAFFIC_FRAME.active!, latency_ms: 40 + i } });
    expect(real()).toHaveTextContent("45 ms");
    expect(vi.mocked(checkedAgo).mock.calls.length).toBe(renders);
    api$.emitTraffic({ ...TRAFFIC_FRAME, ts: TRAFFIC_FRAME.ts + 9_000, active: { ...TRAFFIC_FRAME.active!, real_ok: false } });
    expect(real()).toHaveTextContent("failed");
  });

  it("a checked age keeps ticking on a row that would otherwise stay memoised", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(NOW_SEC * 1000));
    const api$ = mockNodeGroups(mockApi());
    // The shell polls status every 3 s and every resolution recalibrates the gateway clock (recordServerNow);
    // track the fake clock so that recalibration doesn't erase the advance below.
    api$.getStatus.mockImplementation(async () => ({ ...STATUS, server_now: Math.floor(Date.now() / 1000) }));
    renderApp("/nodes");
    await stepUntil(() => document.querySelectorAll("[data-node-id]").length > 0);
    expect(row("nl-ams-03")).toHaveTextContent("5 s ago");
    await act(() => vi.advanceTimersByTimeAsync(2 * 60_000));
    expect(row("nl-ams-03")).toHaveTextContent("2 min ago");
  });
});

describe("Servers › row actions (N6, N7, N15)", () => {
  it("Connect is a connection write: every connection control waits, then it says so", async () => {
    const { api$ } = await openList();
    let finish: () => void = () => {};
    api$.apply.mockImplementation(() => new Promise((resolve) => { finish = () => resolve({ ok: true }); }));
    const success = vi.spyOn(toast, "success");
    await userEvent.click(within(row("de-fra-01")).getByRole("button", { name: "Connect de-fra-01" }));
    expect(api$.apply).toHaveBeenCalledWith(2);
    await waitFor(() => expect(within(row("de-fra-01")).getByRole("button", { name: "Connecting… de-fra-01" })).toBeDisabled());
    expect(within(row("fi-hel-02")).getByRole("button", { name: "Connect fi-hel-02" })).toBeDisabled();
    expect(within(row("nl-ams-03")).getByRole("button", { name: "Disconnect nl-ams-03" })).toBeDisabled();
    await act(async () => finish());
    await waitFor(() => expect(success).toHaveBeenCalledWith("Connected to de-fra-01", { duration: 8000 }));
    await waitFor(() => expect(within(row("fi-hel-02")).getByRole("button", { name: "Connect fi-hel-02" })).toBeEnabled());
  });

  it("Disconnect disconnects the active node; a failure carries the backend's message", async () => {
    const { api$ } = await openList();
    api$.disconnect.mockRejectedValue(new ApiError(409, "node 1 is not connected (active is 2)"));
    const error = vi.spyOn(toast, "error");
    await userEvent.click(within(row("nl-ams-03")).getByRole("button", { name: "Disconnect nl-ams-03" }));
    expect(api$.disconnect).toHaveBeenCalledWith(1);
    await waitFor(() => expect(error).toHaveBeenCalledWith("node 1 is not connected (active is 2)", { duration: 20000 }));
  });

  it("a connection write elsewhere, or an unreachable gateway, disables Connect and says why", async () => {
    const { api$, client } = await openList();
    const release = holdConnectionWrite(client);
    await waitFor(() => expect(within(row("de-fra-01")).getByRole("button", { name: "Connect de-fra-01" })).toBeDisabled());
    expect(within(row("de-fra-01")).getByRole("button", { name: "Connect de-fra-01" })).toHaveAttribute("title", "Another connection change is still running — try again when it finishes");
    await release();
    await waitFor(() => expect(within(row("de-fra-01")).getByRole("button", { name: "Connect de-fra-01" })).toBeEnabled());

    api$.getStatus.mockRejectedValue(new ApiError(0, "network error"));
    await act(() => client.refetchQueries({ queryKey: ["status"] }));
    await waitFor(() => expect(within(row("de-fra-01")).getByRole("button", { name: "Connect de-fra-01" })).toHaveAttribute("title", "Gateway unreachable"));
    expect(within(row("de-fra-01")).getByRole("button", { name: "Connect de-fra-01" })).toBeDisabled();
    expect(screen.getByText("Gateway unreachable — connecting is unavailable until it answers.")).toBeInTheDocument();
  });

  it("Test probes one node, busy meanwhile, and its result lands in the row", async () => {
    const { api$ } = await openList();
    let finish: (health: NodeHealth) => void = () => {};
    api$.probeNode.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    await userEvent.click(within(row("ch-zrh-02")).getByRole("button", { name: "Test ch-zrh-02" }));
    expect(api$.probeNode).toHaveBeenCalledWith(5);
    expect(within(row("ch-zrh-02")).getByRole("button", { name: "Testing ch-zrh-02" })).toBeDisabled();
    const result = { ...NODE_HEALTH[1]!, node_id: 5, last_tcp_ms: 61, last_http_ms: 120, last_real_ms: 77 };
    api$.listNodeHealth.mockResolvedValue([...ALL_NODE_HEALTH, result]);
    await act(async () => finish(result));
    await waitFor(() => expect(within(row("ch-zrh-02")).getByText("77 ms")).toBeInTheDocument());
    expect(within(row("ch-zrh-02")).getByRole("button", { name: "Test ch-zrh-02" })).toBeEnabled();
  });

  it("a failed test says which node", async () => {
    const { api$ } = await openList();
    api$.probeNode.mockRejectedValue(new Error("offline"));
    const error = vi.spyOn(toast, "error");
    await userEvent.click(within(row("de-fra-01")).getByRole("button", { name: "Test de-fra-01" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("test of de-fra-01 failed", { duration: 20000 }));
  });

  it("Servers group: the menu deletes after the contract's confirmation", async () => {
    const { api$ } = await openList("/nodes?group=servers");
    const success = vi.spyOn(toast, "success");
    await userEvent.click(within(row("vps-hel")).getByRole("button", { name: "More actions for vps-hel" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).queryByRole("menuitem", { name: /Detach/ })).toBeNull();
    await userEvent.click(within(menu).getByRole("menuitem", { name: "Delete…" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm" });
    expect(dialog).toHaveTextContent('Delete server "vps-hel" (198.51.100.23)?');
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api$.deleteNode).toHaveBeenCalledWith(7));
    await waitFor(() => expect(success).toHaveBeenCalledWith("Deleted vps-hel", { duration: 8000 }));
  });

  it("declining the confirmation deletes nothing; a 409 explains the active node", async () => {
    const { api$ } = await openList("/nodes?group=servers");
    const error = vi.spyOn(toast, "error");
    await userEvent.click(within(row("lab-lan")).getByRole("button", { name: "More actions for lab-lan" }));
    await userEvent.click(within(await screen.findByRole("menu")).getByRole("menuitem", { name: "Delete…" }));
    await userEvent.click(within(await screen.findByRole("dialog", { name: "Confirm" })).getByRole("button", { name: "Cancel" }));
    expect(api$.deleteNode).not.toHaveBeenCalled();

    api$.deleteNode.mockRejectedValue(new ApiError(409, "disconnect the active node before deleting it"));
    await userEvent.click(within(row("lab-lan")).getByRole("button", { name: "More actions for lab-lan" }));
    await userEvent.click(within(await screen.findByRole("menu")).getByRole("menuitem", { name: "Delete…" }));
    await userEvent.click(within(await screen.findByRole("dialog", { name: "Confirm" })).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("That node is active. Disconnect → Edit → Connect, then try again.", { duration: 20000 }));
  });

  it("the active manual node cannot be deleted: the item says Disconnect first", async () => {
    await openList("/nodes?group=servers", { active_node_id: 8 });
    await userEvent.click(within(row("lab-lan")).getByRole("button", { name: "More actions for lab-lan" }));
    const item = within(await screen.findByRole("menu")).getByRole("menuitem", { name: /Delete/ });
    expect(item).toHaveAttribute("data-disabled");
    expect(item).toHaveTextContent("Disconnect first");
  });

  it("a subscription node is detached to Servers, never deleted", async () => {
    const { api$ } = await openList();
    const success = vi.spyOn(toast, "success");
    await userEvent.click(within(row("de-fra-01")).getByRole("button", { name: "More actions for de-fra-01" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).queryByRole("menuitem", { name: /Delete/ })).toBeNull();
    await userEvent.click(within(menu).getByRole("menuitem", { name: "Detach to Servers" }));
    await waitFor(() => expect(api$.detachNodes).toHaveBeenCalledWith([2]));
    await waitFor(() => expect(success).toHaveBeenCalledWith("Detached de-fra-01 to Servers", { duration: 8000 }));
  });
});

describe("Servers › reorder and row cap (N11, N19)", () => {
  it("arrows move a manual server at once and send the whole group's order", async () => {
    const { api$ } = await openList("/nodes?group=servers");
    let finish: () => void = () => {};
    api$.reorderNodes.mockImplementation(() => new Promise((resolve) => { finish = () => resolve({ ok: true }); }));
    expect(within(row("vps-hel")).getByRole("button", { name: "Move vps-hel up" })).toBeDisabled();
    expect(within(row("kz-ala-01")).getByRole("button", { name: "Move kz-ala-01 down" })).toBeDisabled();
    api$.listNodes.mockResolvedValue([...ALL_NODES.slice(0, 6), ALL_NODES[7]!, ALL_NODES[6]!, ALL_NODES[8]!, ALL_NODES[9]!]);
    await userEvent.click(within(row("lab-lan")).getByRole("button", { name: "Move lab-lan up" }));
    expect(api$.reorderNodes).toHaveBeenCalledWith([8, 7, 9]);
    expect(rowNames()).toEqual(["lab-lan", "vps-hel", "kz-ala-01"]);
    expect(within(row("kz-ala-01")).getByRole("button", { name: "Move kz-ala-01 up" })).toBeDisabled();   // busy
    await act(async () => finish());
    await waitFor(() => expect(within(row("kz-ala-01")).getByRole("button", { name: "Move kz-ala-01 up" })).toBeEnabled());
  });

  it("a failed reorder says so and reloads the real order", async () => {
    const { api$ } = await openList("/nodes?group=servers");
    api$.reorderNodes.mockRejectedValue(new ApiError(500, "reorder broke"));
    const error = vi.spyOn(toast, "error");
    await userEvent.click(within(row("kz-ala-01")).getByRole("button", { name: "Move kz-ala-01 up" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("reorder broke", { duration: 20000 }));
    await waitFor(() => expect(rowNames()).toEqual(["vps-hel", "lab-lan", "kz-ala-01"]));
  });

  it("no arrows outside Servers, with another sort, or while searching", async () => {
    const first = await openList("/nodes");
    expect(screen.queryByRole("button", { name: /^Move / })).toBeNull();
    first.unmount();
    const second = await openList("/nodes?group=servers&sort=name");
    expect(screen.queryByRole("button", { name: /^Move / })).toBeNull();
    second.unmount();
    await openList("/nodes?group=servers&q=vps");
    expect(screen.queryByRole("button", { name: /^Move / })).toBeNull();
  });

  it("renders 100 of 240 servers until show all", async () => {
    const api$ = mockApi();
    api$.listNodes.mockResolvedValue(Array.from({ length: 240 }, (_, i) => node(100 + i, `bulk-${String(i).padStart(3, "0")}`)));
    renderApp("/nodes?group=servers");
    await waitFor(() => expect(rowNames()).toHaveLength(100));
    const footer = screen.getByText(/Showing 100 of 240/);
    await userEvent.click(within(footer).getByRole("button", { name: "show all" }));
    await waitFor(() => expect(rowNames()).toHaveLength(240));
    expect(screen.queryByText(/Showing 100 of 240/)).toBeNull();
  });

  it("an in-flight nodes refetch does not undo an optimistic move; the next move includes it", async () => {
    const { api$, client } = await openList("/nodes?group=servers");
    let resolveRefetch: (nodes: typeof ALL_NODES) => void = () => {};
    api$.listNodes.mockImplementation(() => new Promise((resolve) => { resolveRefetch = resolve; }));
    const refetch = client.refetchQueries({ queryKey: keys.nodes });   // e.g. the 30 s poll landing mid-move
    let finishReorder: () => void = () => {};
    api$.reorderNodes.mockImplementation(() => new Promise((resolve) => { finishReorder = () => resolve({ ok: true }); }));
    await userEvent.click(within(row("kz-ala-01")).getByRole("button", { name: "Move kz-ala-01 up" }));
    expect(api$.reorderNodes).toHaveBeenCalledWith([7, 9, 8]);
    expect(rowNames()).toEqual(["vps-hel", "kz-ala-01", "lab-lan"]);
    resolveRefetch(ALL_NODES);   // the stale refetch resolves after the optimistic update, with the pre-move order
    await act(async () => { await refetch.catch(() => {}); });
    expect(rowNames()).toEqual(["vps-hel", "kz-ala-01", "lab-lan"]);   // still the moved order, not reverted
    await act(async () => finishReorder());
    api$.listNodes.mockResolvedValue(ALL_NODES);
    await userEvent.click(within(row("vps-hel")).getByRole("button", { name: "Move vps-hel down" }));
    expect(api$.reorderNodes).toHaveBeenCalledWith([9, 7, 8]);
  });
});

describe("Servers › phone cards", () => {
  it("one card per node: a link to its page, pills, the best reading, and Connect and Test on the card", async () => {
    setViewportWidth(390);
    const { api$ } = await openList();
    expect(screen.queryByRole("table")).toBeNull();
    const cards = within(screen.getByRole("list", { name: "Nodes" }));
    const card = (name: string) => document.querySelector<HTMLElement>(`li[data-node-name="${name}"]`)!;
    expect(cards.getByRole("link", { name: "de-fra-01" })).toHaveAttribute("href", "/nodes/2");
    await waitFor(() => expect(card("nl-ams-03")).toHaveTextContent("connected · 1m"));
    expect(card("nl-ams-03")).toHaveTextContent("TCP31 msHTTP142 msreal45 ms");
    expect(card("de-fra-01")).toHaveTextContent("real 58 ms · 4 min ago");
    expect(card("se-sto-01")).toHaveTextContent("HTTP 139 ms · 12 min ago");
    expect(card("ch-zrh-02")).toHaveTextContent("not probed");
    expect(within(card("de-fra-01")).queryByRole("button", { name: /More actions/ })).toBeNull();
    await userEvent.click(within(card("de-fra-01")).getByRole("button", { name: "Connect de-fra-01" }));
    expect(api$.apply).toHaveBeenCalledWith(2);
  });
});
