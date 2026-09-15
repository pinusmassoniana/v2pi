import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type Status } from "../../api/client";
import { settleConfirm } from "../../components/confirm";
import { ALL_NODE_HEALTH, STATUS, holdConnectionWrite, mockApi, mockNodeGroups } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";
import { setViewportWidth } from "../../test/viewport";

afterEach(() => act(() => settleConfirm(false)));

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
    expect(screen.getByText(/^connected/)).toHaveTextContent("connected · 1m");
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
    await screen.findByRole("heading", { level: 2, name: "🇺🇸 us-nyc-01" });
    expect(screen.getByRole("link", { name: "home" })).toHaveAttribute("href", "/nodes?group=2");
    expect(screen.getByText("stale")).toBeInTheDocument();
    expect(screen.getByText("id 10")).toBeInTheDocument();
  });
});

describe("Node detail › profile and actions (N6, N7, N15, T6)", () => {
  it("changes the tuning profile in place", async () => {
    const { api$ } = await openDetail("/nodes/2", { phone: true });
    const success = vi.spyOn(toast, "success");
    const row = await screen.findByRole("region", { name: "Tuning profile" });
    await waitFor(() => expect(row).toHaveTextContent("(default) · balanced"));
    await userEvent.click(within(row).getByRole("button", { name: "Change" }));
    await userEvent.selectOptions(within(row).getByRole("combobox", { name: "Tuning profile" }), "fragment-tls");
    expect(api$.updateNode).toHaveBeenCalledWith(2, { tuning_profile_id: 2 });
    await waitFor(() => expect(success).toHaveBeenCalledWith("Assigned fragment-tls to de-fra-01", { duration: 8000 }));
    expect(within(row).queryByRole("combobox")).toBeNull();
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
    await userEvent.selectOptions(within(row).getByRole("combobox", { name: "Tuning profile" }), "(default)");
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

  it("the active node disconnects", async () => {
    const { api$ } = await openDetail("/nodes/1", { phone: true });
    await userEvent.click(await screen.findByRole("button", { name: "Disconnect" }));
    expect(api$.disconnect).toHaveBeenCalledWith(1);
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

  it("the active manual server's Delete is disabled with its reason", async () => {
    await openDetail("/nodes/8", { phone: true, status: { active_node_id: 8 } });
    const actions = await screen.findByRole("region", { name: "Node actions" });
    expect(within(actions).getByRole("button", { name: "Delete" })).toBeDisabled();
    expect(within(actions).getByText("Disconnect first")).toBeInTheDocument();
  });

  it("a subscription node is detached, never deleted", async () => {
    const { api$ } = await openDetail("/nodes/2", { phone: true });
    const actions = await screen.findByRole("region", { name: "Node actions" });
    expect(within(actions).queryByRole("button", { name: "Delete" })).toBeNull();
    await userEvent.click(within(actions).getByRole("button", { name: "Detach" }));
    expect(api$.detachNodes).toHaveBeenCalledWith([2]);
  });
});
