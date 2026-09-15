import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type NodeHealth, type Status } from "../../api/client";
import { settleConfirm } from "../../components/confirm";
import { ALL_NODE_HEALTH, NODE_HEALTH, NOW_SEC, PROFILES, STATUS, holdConnectionWrite, mockApi, mockNodeGroups } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";
import { setViewportWidth } from "../../test/viewport";

afterEach(() => act(() => settleConfirm(false)));
afterEach(() => vi.useRealTimers());

/** On fake timers Testing Library's waitFor never polls: step the clock until `check` holds. */
async function stepUntil(check: () => unknown) {
  for (let i = 0; i < 80 && !check(); i++) await act(() => vi.advanceTimersByTimeAsync(25));
  expect(check()).toBeTruthy();
}

const terms = (region: HTMLElement) => within(region).getAllByRole("term").map((term) => term.textContent);
const value = (region: HTMLElement, key: string) => within(region).getByText(key, { selector: "dt" }).nextElementSibling!;

async function openDetail(path: string, { phone = false, status = {} as Partial<Status> } = {}) {
  if (phone) setViewportWidth(390);
  const api$ = mockNodeGroups(mockApi());
  api$.getStatus.mockResolvedValue({ ...STATUS, ...status });
  const view = renderApp(path);
  return { api$, ...view };
}

describe("Node detail › desktop sheet", () => {
  it("opens over the list of the node's group, and closing returns to that list with its search and sort", async () => {
    const { router } = await openDetail("/nodes/2?q=example&sort=name");
    const sheet = await screen.findByRole("dialog", { name: "de-fra-01" });
    expect(within(sheet).getByRole("region", { name: "Health" })).toBeInTheDocument();
    await waitFor(() => expect(document.querySelectorAll("tr[data-node-id]")).toHaveLength(6));
    expect(within(screen.getByRole("navigation", { name: "Node groups", hidden: true })).getByRole("link", { name: "work 6", hidden: true })).toHaveAttribute("aria-current", "page");
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(router.state.location.pathname).toBe("/nodes"));
    expect(router.state.location.search).toEqual({ group: 1, q: "example", sort: "name" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("a manual server's detail shows the Servers list behind it", async () => {
    await openDetail("/nodes/7");
    await screen.findByRole("dialog", { name: "vps-hel" });
    await waitFor(() => expect([...document.querySelectorAll<HTMLElement>("tr[data-node-id]")].map((row) => row.dataset.nodeName)).toEqual(["vps-hel", "lab-lan", "kz-ala-01"]));
  });

  it("an unknown node says so, with a way back to Servers", async () => {
    await openDetail("/nodes/42");
    const sheet = await screen.findByRole("dialog", { name: "Node not found" });
    expect(within(sheet).getByText("Node not found", { selector: "p" })).toBeInTheDocument();
    expect(within(sheet).getByRole("link", { name: "Back to Servers" })).toHaveAttribute("href", "/nodes");
  });
});

describe("Node detail › phone page", () => {
  it("is a page with its own heading and a way back to the node's group", async () => {
    await openDetail("/nodes/2?sort=tcp", { phone: true });
    expect(await screen.findByRole("heading", { level: 2, name: "🇩🇪 de-fra-01" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(await screen.findByRole("link", { name: "Servers · work" })).toHaveAttribute("href", "/nodes?group=1&sort=tcp");
    expect(screen.getByRole("heading", { level: 3, name: "Health" })).toBeInTheDocument();
  });

  it("an unknown node is an empty state on the page", async () => {
    await openDetail("/nodes/abc", { phone: true });
    expect(await screen.findByText("Node not found")).toBeInTheDocument();
  });
});

describe("Node detail › health and config (N5)", () => {
  it("health: probe pills with their age, egress with flags, the trend; the failure counter only on the active node", async () => {
    setViewportWidth(390);
    const api$ = mockNodeGroups(mockApi());
    api$.listNodeHealth.mockResolvedValue(ALL_NODE_HEALTH.map((h) => (h.node_id === 1 ? { ...h, fail_count: 2, egress_ip6: "2a0b:4d07::21", egress_cc6: "NL" } : h)));
    renderApp("/nodes/1");
    const health = await screen.findByRole("region", { name: "Health" });
    await waitFor(() => expect(value(health, "Failures")).toHaveTextContent("2"));
    expect(health).toHaveTextContent("checked 5 s ago");
    expect(terms(health)).toEqual(["TCP", "HTTP", "Real", "Failures", "Egress"]);
    expect(value(health, "TCP")).toHaveTextContent("31 ms");
    expect(value(health, "Egress")).toHaveTextContent("🇳🇱 185.107.56.21🇳🇱 2a0b:4d07::21");
    expect(health).toHaveTextContent("Latency trend · last 2 samples");
    // Scoped to the page's own header: the hidden list underneath shows the same active row's "connected" text.
    const header = (await screen.findByRole("heading", { level: 2, name: "🇳🇱 nl-ams-03" })).closest("header")!;
    expect(within(header).getByText(/^connected/)).toHaveTextContent("connected · 1m");
  });

  it("the active node's header: active · not running with xray stopped, active · unknown while status fails", async () => {
    const { api$, client } = await openDetail("/nodes/1", { phone: true, status: { running: false } });
    const header = (await screen.findByRole("heading", { level: 2, name: "🇳🇱 nl-ams-03" })).closest("header")!;
    await waitFor(() => expect(within(header).getByText(/^active/)).toHaveTextContent("active · not running · —"));
    api$.getStatus.mockRejectedValue(new ApiError(0, "network error"));
    await act(() => client.refetchQueries({ queryKey: ["status"] }));
    await waitFor(() => expect(within(header).getByText(/^active/)).toHaveTextContent("active · unknown · —"));
    expect(within(header).queryByText(/^connected/)).toBeNull();
  });

  it("node health that failed to load is an error with Retry in place of the health card", async () => {
    setViewportWidth(390);
    const api$ = mockNodeGroups(mockApi());
    api$.listNodeHealth.mockRejectedValue(new ApiError(500, "boom"));
    renderApp("/nodes/2");
    const page = (await screen.findByRole("region", { name: "Config" })).parentElement!;
    const error = await within(page).findByText("Node health did not load", {}, { timeout: 3000 });
    expect(within(page).queryByRole("region", { name: "Health" })).toBeNull();
    api$.listNodeHealth.mockResolvedValue(ALL_NODE_HEALTH);
    await userEvent.click(within(error.closest<HTMLElement>("[role=alert]")!).getByRole("button", { name: "Retry" }));
    expect(await within(page).findByRole("region", { name: "Health" })).toHaveTextContent("checked 4 min ago");
  });

  it("a node never probed says so, and a standby has no failure counter", async () => {
    await openDetail("/nodes/5", { phone: true });
    const health = await screen.findByRole("region", { name: "Health" });
    expect(health).toHaveTextContent("not probed");
    expect(health).toHaveTextContent("Not probed yet — Test runs TCP, HTTP and a real request through this node.");
    expect(terms(health)).not.toContain("Failures");
  });

  it("config shows reality keys, TLS ALPN and xhttp fields only for their kind", async () => {
    const first = await openDetail("/nodes/1", { phone: true });
    let config = await screen.findByRole("region", { name: "Config" });
    expect(terms(config)).toEqual(["Address", "Port", "Transport", "Security", "SNI", "Public key", "Short ID", "Fingerprint", "Flow", "Note"]);
    expect(value(config, "Flow")).toHaveTextContent("xtls-rprx-vision");
    first.unmount();

    const second = await openDetail("/nodes/3", { phone: true });
    config = await screen.findByRole("region", { name: "Config" });
    expect(terms(config)).toEqual(["Address", "Port", "Transport", "Security", "SNI", "Public key", "Short ID", "Path", "Host", "Mode", "Fingerprint", "Flow", "Note"]);
    expect(value(config, "Path")).toHaveTextContent("/xh");
    expect(value(config, "Flow")).toHaveTextContent("—");
    second.unmount();

    await openDetail("/nodes/4", { phone: true });
    config = await screen.findByRole("region", { name: "Config" });
    expect(terms(config)).toEqual(["Address", "Port", "Transport", "Security", "SNI", "ALPN", "Path", "Host", "Mode", "Fingerprint", "Flow", "Note"]);
    expect(value(config, "ALPN")).toHaveTextContent("h2,http/1.1");
  });

  it("the header names the group, the stale badge and the kind", async () => {
    await openDetail("/nodes/10", { phone: true });
    const heading = await screen.findByRole("heading", { level: 2, name: "🇺🇸 us-nyc-01" });
    // Scoped to the page's own header: the hidden list underneath renders the same node's stale badge too.
    const header = heading.closest("header")!;
    expect(within(header).getByRole("link", { name: "home" })).toHaveAttribute("href", "/nodes?group=2");
    expect(within(header).getByText("stale")).toBeInTheDocument();
    expect(within(header).getByText("id 10")).toBeInTheDocument();
  });
});

describe("Node detail › profile and actions (N6, N7, N15, T6)", () => {
  it("changes the tuning profile in place: a pick is staged, Save sends it", async () => {
    const { api$ } = await openDetail("/nodes/2", { phone: true });
    const success = vi.spyOn(toast, "success");
    const row = await screen.findByRole("region", { name: "Tuning profile" });
    await waitFor(() => expect(row).toHaveTextContent("(global default) · balanced"));
    await userEvent.click(within(row).getByRole("button", { name: "Change" }));
    const select = within(row).getByRole("combobox", { name: "Tuning profile" });
    expect(within(select).getAllByRole("option").map((option) => option.textContent)).toEqual(["(global default)", "balanced", "fragment-tls"]);
    await userEvent.selectOptions(select, "fragment-tls");
    expect(api$.updateNode).not.toHaveBeenCalled();
    await userEvent.click(within(row).getByRole("button", { name: "Save" }));
    expect(api$.updateNode).toHaveBeenCalledWith(2, { tuning_profile_id: 2 });
    await waitFor(() => expect(success).toHaveBeenCalledWith("Assigned fragment-tls to de-fra-01", { duration: 8000 }));
    expect(within(row).queryByRole("combobox")).toBeNull();
  });

  it("arrow keys on the closed select only stage a profile; Enter saves it", async () => {
    const { api$ } = await openDetail("/nodes/2", { phone: true });
    const row = await screen.findByRole("region", { name: "Tuning profile" });
    await userEvent.click(within(row).getByRole("button", { name: "Change" }));
    const select = within(row).getByRole("combobox", { name: "Tuning profile" });
    await waitFor(() => expect(within(select).getByRole("option", { name: "fragment-tls" })).toBeInTheDocument());
    expect(select).toHaveValue("");
    // A closed select moves its value on ArrowDown and fires change for each step, with no click or blur.
    select.focus();
    for (const value of ["1", "2"]) fireEvent.change(select, { target: { value } });
    expect(select).toHaveValue("2");
    expect(api$.updateNode).not.toHaveBeenCalled();
    await userEvent.keyboard("{Enter}");
    expect(api$.updateNode).toHaveBeenCalledWith(2, { tuning_profile_id: 2 });
  });

  it("the node's own profile shows once the profiles load, and the row locks if the node turns active meanwhile", async () => {
    const { api$, client } = await openDetail("/nodes/1", { phone: true, status: { active_node_id: 2 } });   // nl-ams-03 uses fragment-tls
    let loadProfiles: (profiles: typeof PROFILES) => void = () => {};
    api$.listProfiles.mockImplementation(() => new Promise((resolve) => { loadProfiles = resolve; }));
    const row = await screen.findByRole("region", { name: "Tuning profile" });
    await userEvent.click(within(row).getByRole("button", { name: "Change" }));
    const select = within(row).getByRole("combobox", { name: "Tuning profile" });
    await act(async () => loadProfiles(PROFILES));
    await waitFor(() => expect(select).toHaveValue("2"));

    api$.getStatus.mockResolvedValue({ ...STATUS, active_node_id: 1 });
    await act(() => client.refetchQueries({ queryKey: ["status"] }));
    await waitFor(() => expect(select).toBeDisabled());
    expect(within(row).getByRole("button", { name: "Save" })).toBeDisabled();
    expect(within(row).getByRole("button", { name: "Cancel" })).toBeEnabled();
  });

  it("the active node's profile cannot change here: Change is disabled and says Disconnect first", async () => {
    await openDetail("/nodes/1", { phone: true });
    const row = await screen.findByRole("region", { name: "Tuning profile" });
    await waitFor(() => expect(row).toHaveTextContent("fragment-tls"));
    expect(within(row).getByRole("button", { name: "Change" })).toBeDisabled();
    expect(within(row).getByText("Disconnect first")).toBeInTheDocument();
  });

  it("a profile change refused as active explains it", async () => {
    const { api$ } = await openDetail("/nodes/2", { phone: true });
    api$.updateNode.mockRejectedValue(new ApiError(409, "disconnect the active node before editing it"));
    const error = vi.spyOn(toast, "error");
    const row = await screen.findByRole("region", { name: "Tuning profile" });
    await userEvent.click(within(row).getByRole("button", { name: "Change" }));
    await userEvent.selectOptions(within(row).getByRole("combobox", { name: "Tuning profile" }), "(global default)");
    await userEvent.click(within(row).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("That node is active. Disconnect → Edit → Connect, then try again.", { duration: 20000 }));
  });

  it("Connect and Test; a disabled Connect says why", async () => {
    const { api$, client } = await openDetail("/nodes/2", { phone: true });
    await userEvent.click(await screen.findByRole("button", { name: "Connect" }));
    expect(api$.apply).toHaveBeenCalledWith(2);
    await userEvent.click(screen.getByRole("button", { name: "Test" }));
    expect(api$.probeNode).toHaveBeenCalledWith(2);
    await waitFor(() => expect(screen.getByRole("button", { name: "Connect" })).toBeEnabled());

    const release = holdConnectionWrite(client);
    expect(await screen.findByText("Another connection change is still running — try again when it finishes")).toBeInTheDocument();
    await release();
    api$.getStatus.mockRejectedValue(new ApiError(0, "network error"));
    await act(() => client.refetchQueries({ queryKey: ["status"] }));
    expect(await screen.findByText("Gateway unreachable")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect" })).toBeDisabled();
  });

  it("the active node disconnects after the same question as Home", async () => {
    const { api$ } = await openDetail("/nodes/1", { phone: true });
    await userEvent.click(await screen.findByRole("button", { name: "Disconnect" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm" });
    expect(dialog).toHaveTextContent("Disconnect from nl-ams-03? Devices lose the tunnel until a node is connected again.");
    expect(api$.disconnect).not.toHaveBeenCalled();
    await userEvent.click(within(dialog).getByRole("button", { name: "Disconnect" }));
    await waitFor(() => expect(api$.disconnect).toHaveBeenCalledWith(1));
  });

  it("closing the sheet replaces the node's history entry, so Back does not open it again", async () => {
    mockNodeGroups(mockApi());
    const { router } = renderApp("/nodes");
    await waitFor(() => expect(document.querySelectorAll("[data-node-id]").length).toBeGreaterThan(0));
    await userEvent.click(screen.getByRole("link", { name: "de-fra-01" }));
    await screen.findByRole("dialog", { name: "de-fra-01" });
    const length = router.history.length;
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(router.state.location.pathname).toBe("/nodes"));
    expect(router.history.length).toBe(length);
    await act(async () => router.history.back());
    await waitFor(() => expect(router.state.location.pathname).toBe("/nodes"));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("a manual server is deleted after the confirmation, back to Servers", async () => {
    const { api$, router } = await openDetail("/nodes/7", { phone: true });
    const actions = await screen.findByRole("region", { name: "Node actions" });
    expect(within(actions).queryByRole("button", { name: "Detach" })).toBeNull();
    await userEvent.click(within(actions).getByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm" });
    expect(dialog).toHaveTextContent('Delete server "vps-hel" (198.51.100.23)?');
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api$.deleteNode).toHaveBeenCalledWith(7));
    await waitFor(() => expect(router.state.location.pathname).toBe("/nodes"));
    expect(router.state.location.search).toEqual({ group: "servers" });
  });

  it("the active manual server's Edit and Delete are disabled with their reason", async () => {
    await openDetail("/nodes/8", { phone: true, status: { active_node_id: 8 } });
    const actions = await screen.findByRole("region", { name: "Node actions" });
    expect(within(actions).getByRole("button", { name: "Delete" })).toBeDisabled();
    expect(within(actions).getByRole("button", { name: "Edit" })).toBeDisabled();
    expect(within(actions).getAllByText("Disconnect first")).toHaveLength(2);   // under Edit and under Delete
    expect(within(actions).getByRole("button", { name: "Clone" })).toBeEnabled();
  });

  it("a subscription node is detached, never deleted", async () => {
    const { api$ } = await openDetail("/nodes/2", { phone: true });
    const actions = await screen.findByRole("region", { name: "Node actions" });
    expect(within(actions).queryByRole("button", { name: "Delete" })).toBeNull();
    await userEvent.click(within(actions).getByRole("button", { name: "Detach" }));
    expect(api$.detachNodes).toHaveBeenCalledWith([2]);
  });
});

// The /nodes layout keeps the Servers list mounted under a node's sheet or page (round-1 fix): opening or
// closing a node must not restart a running sweep, drop the selection, or strand focus outside the document.
describe("Node detail › the list survives opening and closing a node (fix round 1)", () => {
  it("a running Test all keeps scheduling probes across opening and closing a node's sheet", async () => {
    const api$ = mockNodeGroups(mockApi());
    const pending: ((health: NodeHealth) => void)[] = [];
    api$.probeNode.mockImplementation(() => new Promise((resolve) => { pending.push(resolve); }));
    const { router } = renderApp("/nodes");
    await waitFor(() => expect(document.querySelectorAll("[data-node-id]").length).toBeGreaterThan(0));
    const toolbar = within(screen.getByRole("region", { name: "Servers toolbar" }));
    await userEvent.click(toolbar.getByRole("button", { name: "Test all (real)" }));
    expect(api$.probeNode).toHaveBeenCalledTimes(1);

    await act(() => router.navigate({ to: "/nodes/$nodeId", params: { nodeId: "2" } }));
    await screen.findByRole("dialog", { name: "de-fra-01" });
    await act(async () => pending.shift()!(NODE_HEALTH[0]!));
    // The sweep kept scheduling its next probe while the sheet was open — it did not see the list as gone.
    // hidden: true — Radix marks the list aria-hidden while its modal sheet is open.
    await waitFor(() => expect(api$.probeNode).toHaveBeenCalledTimes(2));
    expect(toolbar.getByRole("button", { name: /^Testing/, hidden: true })).toBeDisabled();

    await act(() => router.navigate({ to: "/nodes" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    for (let i = 0; i < 5; i++) await act(async () => pending.shift()!(NODE_HEALTH[0]!));   // nodes 2 through 6
    await waitFor(() => expect(toolbar.getByRole("button", { name: "Test all (real)" })).toBeEnabled());
    expect(api$.probeNode).toHaveBeenCalledTimes(6);   // all six of "work"'s nodes, uninterrupted by the detour
  });

  it("a selection on the list survives opening and closing a node", async () => {
    mockNodeGroups(mockApi());
    const { router } = renderApp("/nodes");
    await waitFor(() => expect(document.querySelectorAll("[data-node-id]").length).toBeGreaterThan(0));
    await userEvent.click(screen.getByRole("checkbox", { name: "Select de-fra-01" }));
    await screen.findByRole("region", { name: "Selection" });

    await act(() => router.navigate({ to: "/nodes/$nodeId", params: { nodeId: "2" } }));
    await screen.findByRole("dialog", { name: "de-fra-01" });
    await act(() => router.navigate({ to: "/nodes" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    expect(screen.getByRole("region", { name: "Selection" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Select de-fra-01" })).toBeChecked();
  });

  it("closing the sheet with Escape returns focus to the node's row link", async () => {
    mockNodeGroups(mockApi());
    renderApp("/nodes");
    await waitFor(() => expect(document.querySelectorAll("[data-node-id]").length).toBeGreaterThan(0));
    const link = screen.getByRole("link", { name: "de-fra-01" });
    await userEvent.click(link);   // the same row link the list never unmounts, so Radix can restore focus to it
    await screen.findByRole("dialog", { name: "de-fra-01" });
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(link);
  });

  it("the checked age keeps ticking while the sheet stays open, without a page-wide re-render", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(NOW_SEC * 1000));
    const api$ = mockNodeGroups(mockApi());
    // The shell polls status every 3 s and every resolution recalibrates the gateway clock (recordServerNow);
    // track the fake clock so that recalibration doesn't erase the advance below.
    api$.getStatus.mockImplementation(async () => ({ ...STATUS, server_now: Math.floor(Date.now() / 1000) }));
    renderApp("/nodes/1");
    await stepUntil(() => screen.queryByRole("dialog", { name: "nl-ams-03" }));
    const health = screen.getByRole("region", { name: "Health" });
    expect(health).toHaveTextContent("checked 5 s ago");
    await act(() => vi.advanceTimersByTimeAsync(15_000));
    expect(health).toHaveTextContent("checked 20 s ago");
  });
});
