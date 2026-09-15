import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type Status } from "../../api/client";
import { settleConfirm } from "../../components/confirm";
import { ALL_NODES, STATUS, mockApi, mockNodeGroups } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";
import { setViewportWidth } from "../../test/viewport";
import { vlessUri } from "./vless";

const writeText = vi.fn<(text: string) => Promise<void>>();

beforeEach(() => {
  writeText.mockReset().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
});
afterEach(() => act(() => settleConfirm(false)));

const row = (name: string) => document.querySelector<HTMLElement>(`tr[data-node-name="${name}"]`)!;

async function openList(path: string, status: Partial<Status> = {}) {
  const api$ = mockNodeGroups(mockApi());
  api$.getStatus.mockResolvedValue({ ...STATUS, ...status });
  const view = renderApp(path);
  await waitFor(() => expect(document.querySelectorAll("[data-node-id]").length).toBeGreaterThan(0));
  return { api$, ...view };
}

async function fromMenu(name: string, item: string) {
  await userEvent.click(within(row(name)).getByRole("button", { name: `More actions for ${name}` }));
  await userEvent.click(within(await screen.findByRole("menu")).getByRole("menuitem", { name: item }));
}

async function fillRequired(sheet: HTMLElement) {
  await userEvent.type(within(sheet).getByLabelText("Name"), "vps-ams-02");
  await userEvent.type(within(sheet).getByLabelText("Address"), "198.51.100.77");
  await userEvent.type(within(sheet).getByLabelText("UUID"), "3f1c9a52-7b1e-4c7d-9a0e-5d2c1b8e4f10");
}

describe("Add server (N12)", () => {
  it("is only offered in the Servers group", async () => {
    await openList("/nodes");
    expect(screen.queryByRole("button", { name: "Add server" })).toBeNull();
    expect(screen.getByText("Add server and Import live in the Servers group")).toBeInTheDocument();
  });

  it("shows path, host and mode for xhttp, the reality keys for reality and ALPN for tls", async () => {
    await openList("/nodes?group=servers");
    await userEvent.click(screen.getByRole("button", { name: "Add server" }));
    const sheet = await screen.findByRole("dialog", { name: "Add server" });
    expect(within(sheet).getByRole("radio", { name: "vision" })).toBeChecked();
    expect(within(sheet).getByRole("radio", { name: "reality" })).toBeChecked();
    expect(within(sheet).getByLabelText("Public key")).toBeInTheDocument();
    expect(within(sheet).getByLabelText("Short ID")).toBeInTheDocument();
    expect(within(sheet).queryByLabelText("ALPN")).toBeNull();
    expect(within(sheet).queryByLabelText("Path")).toBeNull();
    expect(within(sheet).queryByLabelText("Fingerprint")).toBeNull();
    expect(within(sheet).queryByLabelText("Tuning profile")).toBeNull();
    await userEvent.click(within(sheet).getByRole("radio", { name: "xhttp" }));
    for (const label of ["Path", "Host", "Mode"]) expect(within(sheet).getByLabelText(label)).toBeInTheDocument();
    await userEvent.click(within(sheet).getByRole("radio", { name: "tls" }));
    expect(within(sheet).getByLabelText("ALPN")).toBeInTheDocument();
    expect(within(sheet).queryByLabelText("Public key")).toBeNull();
    expect(within(sheet).getByLabelText("SNI")).toBeInTheDocument();
  });

  it("Validate checks the form first, then asks the gateway without saving or refreshing anything", async () => {
    const { api$, client } = await openList("/nodes?group=servers");
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await userEvent.click(screen.getByRole("button", { name: "Add server" }));
    const sheet = await screen.findByRole("dialog", { name: "Add server" });
    await userEvent.click(within(sheet).getByRole("button", { name: "Validate" }));
    expect(within(sheet).getByRole("status")).toHaveTextContent("✗ name is required");
    expect(api$.validateNode).not.toHaveBeenCalled();

    await fillRequired(sheet);
    await userEvent.click(within(sheet).getByRole("button", { name: "Validate" }));
    await waitFor(() => expect(within(sheet).getByRole("status")).toHaveTextContent("✓ config valid"));
    expect(api$.validateNode).toHaveBeenCalledWith(expect.objectContaining({ name: "vps-ams-02", port: 443, transport: "vision", security: "reality" }));
    expect(api$.validateNode.mock.calls[0]![0]).not.toHaveProperty("tuning_profile_id");

    api$.validateNode.mockResolvedValue({ ok: false, error: "reality: invalid public key" });
    await userEvent.click(within(sheet).getByRole("button", { name: "Validate" }));
    await waitFor(() => expect(within(sheet).getByRole("status")).toHaveTextContent("✗ reality: invalid public key"));
    expect(api$.addNode).not.toHaveBeenCalled();
    expect(invalidate).not.toHaveBeenCalled();
  });

  it("adds the server with the hidden fields empty, then shows the Servers group", async () => {
    const { api$, router } = await openList("/nodes?group=servers&q=vps");
    const success = vi.spyOn(toast, "success");
    await userEvent.click(screen.getByRole("button", { name: "Add server" }));
    const sheet = await screen.findByRole("dialog", { name: "Add server" });
    await fillRequired(sheet);
    await userEvent.type(within(sheet).getByLabelText("Public key"), "pbk");
    await userEvent.click(within(sheet).getByRole("radio", { name: "tls" }));
    await userEvent.type(within(sheet).getByLabelText("ALPN"), "h2");
    await userEvent.clear(within(sheet).getByLabelText("Port"));
    await userEvent.type(within(sheet).getByLabelText("Port"), "8443");
    await userEvent.click(within(sheet).getByRole("button", { name: "Add server" }));
    await waitFor(() => expect(api$.addNode).toHaveBeenCalledWith({
      name: "vps-ams-02", address: "198.51.100.77", port: 8443, uuid: "3f1c9a52-7b1e-4c7d-9a0e-5d2c1b8e4f10", transport: "vision",
      security: "tls", sni: "", public_key: "", short_id: "", alpn: "h2", path: "", host: "", mode: "", note: "", fingerprint: "chrome",
    }));
    await waitFor(() => expect(success).toHaveBeenCalledWith("Added vps-ams-02", { duration: 8000 }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Add server" })).toBeNull());
    expect(router.state.location.search).toEqual({ group: "servers" });
    expect(screen.queryByRole("dialog", { name: "Confirm" })).toBeNull();
  });

  it("a port out of range is an error on the field and nothing is sent", async () => {
    const { api$ } = await openList("/nodes?group=servers");
    await userEvent.click(screen.getByRole("button", { name: "Add server" }));
    const sheet = await screen.findByRole("dialog", { name: "Add server" });
    await fillRequired(sheet);
    await userEvent.clear(within(sheet).getByLabelText("Port"));
    await userEvent.type(within(sheet).getByLabelText("Port"), "70000");
    await userEvent.click(within(sheet).getByRole("button", { name: "Add server" }));
    expect(await within(sheet).findByText("port must be 1–65535")).toBeInTheDocument();
    expect(within(sheet).getByLabelText("Port")).toHaveAttribute("aria-invalid", "true");
    expect(api$.addNode).not.toHaveBeenCalled();
  });

  it("a server with the same identity is refused in the form, which keeps what was typed", async () => {
    const { api$ } = await openList("/nodes?group=servers");
    api$.addNode.mockRejectedValue(new ApiError(409, "a node with this identity already exists"));
    await userEvent.click(screen.getByRole("button", { name: "Add server" }));
    const sheet = await screen.findByRole("dialog", { name: "Add server" });
    await fillRequired(sheet);
    await userEvent.click(within(sheet).getByRole("button", { name: "Add server" }));
    expect(await within(sheet).findByRole("alert")).toHaveTextContent("a node with this identity already exists");
    expect(within(sheet).getByLabelText("Name")).toHaveValue("vps-ams-02");
  });

  it("closing with unsaved edits asks first; a clean form closes at once", async () => {
    await openList("/nodes?group=servers");
    await userEvent.click(screen.getByRole("button", { name: "Add server" }));
    await screen.findByRole("dialog", { name: "Add server" });
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Add server" })).toBeNull());

    await userEvent.click(screen.getByRole("button", { name: "Add server" }));
    const sheet = await screen.findByRole("dialog", { name: "Add server" });
    await userEvent.type(within(sheet).getByLabelText("Name"), "draft");
    await userEvent.keyboard("{Escape}");
    const ask = await screen.findByRole("dialog", { name: "Confirm" });
    expect(ask).toHaveTextContent("Discard unsaved changes?");
    await userEvent.click(within(ask).getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("dialog", { name: "Add server" })).toBeInTheDocument();
    await userEvent.click(within(sheet).getByRole("button", { name: "Cancel" }));
    await userEvent.click(within(await screen.findByRole("dialog", { name: "Confirm" })).getByRole("button", { name: "Discard" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Add server" })).toBeNull());
  });

  it("leaving the screen with an unsaved form asks before discarding it", async () => {
    const { router } = await openList("/nodes?group=servers");
    await userEvent.click(screen.getByRole("button", { name: "Add server" }));
    await userEvent.type(within(await screen.findByRole("dialog", { name: "Add server" })).getByLabelText("Name"), "draft");
    act(() => void router.navigate({ to: "/tunnel/routing" }));
    const ask = await screen.findByRole("dialog", { name: "Confirm" });
    expect(ask).toHaveTextContent("Discard unsaved changes and leave this screen?");
    await userEvent.click(within(ask).getByRole("button", { name: "Cancel" }));
    expect(router.state.location.pathname).toBe("/nodes");
  });

  it("phone: Add server is in the header menu", async () => {
    setViewportWidth(390);
    await openList("/nodes?group=servers");
    await userEvent.click(screen.getByRole("button", { name: "Add or import servers" }));
    await userEvent.click(within(await screen.findByRole("menu")).getByRole("menuitem", { name: "Add server" }));
    expect(await screen.findByRole("dialog", { name: "Add server" })).toBeInTheDocument();
  });
});

describe("Clone (N14)", () => {
  it("opens Add with the node's fields and name + copy, but not its tuning profile", async () => {
    const { api$ } = await openList("/nodes");
    await fromMenu("nl-ams-03", "Clone");
    const sheet = await screen.findByRole("dialog", { name: "Add server" });
    expect(sheet).toHaveTextContent("Prefilled from nl-ams-03 except the tuning profile");
    expect(within(sheet).getByLabelText("Name")).toHaveValue("nl-ams-03 copy");
    expect(within(sheet).getByLabelText("Address")).toHaveValue("nl-ams-03.example.org");
    expect(within(sheet).getByLabelText("Public key")).toHaveValue("Zm9vX3JlYWxpdHlfcHViX2tleQ");
    expect(within(sheet).getByLabelText("SNI")).toHaveValue("www.microsoft.com");
    expect(api$.addNode).not.toHaveBeenCalled();
    await userEvent.click(within(sheet).getByRole("button", { name: "Add server" }));
    await waitFor(() => expect(api$.addNode).toHaveBeenCalledWith(expect.objectContaining({ name: "nl-ams-03 copy", short_id: "6ba85179e3" })));
    expect(api$.addNode.mock.calls[0]![0]).not.toHaveProperty("tuning_profile_id");
  });
});

describe("Edit node (N13, T6)", () => {
  it("edits every field plus fingerprint and tuning profile, and validates with the profile", async () => {
    const { api$, router } = await openList("/nodes");
    const success = vi.spyOn(toast, "success");
    await fromMenu("fi-hel-02", "Edit");
    const sheet = await screen.findByRole("dialog", { name: "Edit node · fi-hel-02" });
    expect(within(sheet).getByRole("radio", { name: "xhttp" })).toBeChecked();
    expect(within(sheet).getByLabelText("Path")).toHaveValue("/xh");
    expect(within(sheet).getByLabelText("Fingerprint")).toHaveValue("chrome");
    const profile = within(sheet).getByLabelText("Tuning profile");
    await waitFor(() => expect(within(profile).getByRole("option", { name: "fragment-tls" })).toBeInTheDocument());
    await userEvent.selectOptions(profile, "fragment-tls");
    await userEvent.type(within(sheet).getByLabelText("Note"), "fast EU");
    expect(within(sheet).getByText("● unsaved changes")).toBeInTheDocument();
    await userEvent.click(within(sheet).getByRole("button", { name: "Validate" }));
    await waitFor(() => expect(api$.validateNode).toHaveBeenCalledWith(expect.objectContaining({ name: "fi-hel-02", tuning_profile_id: 2 })));
    await userEvent.click(within(sheet).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api$.updateNode).toHaveBeenCalledWith(3, expect.objectContaining({ note: "fast EU", path: "/xh", host: "cdn.example.net", tuning_profile_id: 2 })));
    await waitFor(() => expect(success).toHaveBeenCalledWith("Saved fi-hel-02", { duration: 8000 }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: /Edit node/ })).toBeNull());
    expect(router.state.location.search).toEqual({});
  });

  it("a 409 explains the active node and offers Disconnect in the form", async () => {
    const { api$ } = await openList("/nodes");
    api$.updateNode.mockRejectedValue(new ApiError(409, "disconnect the active node before editing it"));
    await fromMenu("de-fra-01", "Edit");
    const sheet = await screen.findByRole("dialog", { name: "Edit node · de-fra-01" });
    await userEvent.click(within(sheet).getByRole("button", { name: "Save" }));
    const banner = await within(sheet).findByRole("alert");
    expect(banner).toHaveTextContent("That node is active. Disconnect → Edit → Connect, then try again.");
    await userEvent.click(within(banner).getByRole("button", { name: "Disconnect" }));
    expect(api$.disconnect).toHaveBeenCalledWith(2);
  });

  it("the active node's Edit is disabled in the menu with its reason", async () => {
    await openList("/nodes");
    await userEvent.click(within(row("nl-ams-03")).getByRole("button", { name: "More actions for nl-ams-03" }));
    const item = within(await screen.findByRole("menu")).getByRole("menuitem", { name: /Edit/ });
    expect(item).toHaveAttribute("data-disabled");
    expect(item).toHaveTextContent("Disconnect first");
  });

  it("the node's stored tuning profile shows once the profiles load", async () => {
    await openList("/nodes", { active_node_id: 2 });
    await fromMenu("nl-ams-03", "Edit");
    const sheet = await screen.findByRole("dialog", { name: "Edit node · nl-ams-03" });
    await waitFor(() => expect(within(sheet).getByLabelText("Tuning profile")).toHaveValue("2"));
    expect(within(sheet).queryByText("● unsaved changes")).toBeNull();
  });

  it("the node's page opens the same form over its sheet", async () => {
    const { api$ } = await openList("/nodes/7");
    const detail = await screen.findByRole("dialog", { name: "vps-hel" });
    await userEvent.click(within(detail).getByRole("button", { name: "Edit" }));
    const sheet = await screen.findByRole("dialog", { name: "Edit node · vps-hel" });
    await userEvent.click(within(sheet).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api$.updateNode).toHaveBeenCalledWith(7, expect.objectContaining({ name: "vps-hel" })));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Edit node · vps-hel" })).toBeNull());
    expect(screen.getByRole("dialog", { name: "vps-hel" })).toBeInTheDocument();
  });
});

describe("Import (N17)", () => {
  it("imports pasted servers and reports added of total with the format", async () => {
    const { api$ } = await openList("/nodes?group=servers");
    const success = vi.spyOn(toast, "success");
    await userEvent.click(screen.getByRole("button", { name: "Import" }));
    const sheet = await screen.findByRole("dialog", { name: "Import servers" });
    const importButton = within(sheet).getByRole("button", { name: "Import" });
    expect(importButton).toBeDisabled();
    await userEvent.type(within(sheet).getByLabelText("Nodes to import"), "proxies:");
    await userEvent.click(importButton);
    expect(api$.importNodes).toHaveBeenCalledWith("proxies:");
    await waitFor(() => expect(success).toHaveBeenCalledWith("imported 2/3 node(s) (clash)", { duration: 8000 }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Import servers" })).toBeNull());
  });

  it("a parse failure is shown in the sheet and the text stays", async () => {
    const { api$ } = await openList("/nodes?group=servers");
    api$.importNodes.mockRejectedValue(new ApiError(422, "parse failed: no nodes found in input"));
    await userEvent.click(screen.getByRole("button", { name: "Import" }));
    const sheet = await screen.findByRole("dialog", { name: "Import servers" });
    await userEvent.type(within(sheet).getByLabelText("Nodes to import"), "garbage");
    await userEvent.click(within(sheet).getByRole("button", { name: "Import" }));
    expect(await within(sheet).findByRole("alert")).toHaveTextContent("parse failed: no nodes found in input");
    expect(within(sheet).getByLabelText("Nodes to import")).toHaveValue("garbage");
  });
});

describe("Export (N16)", () => {
  it("shows the vless link and the JSON, each copied on its own", async () => {
    await openList("/nodes");
    const success = vi.spyOn(toast, "success");
    await fromMenu("nl-ams-03", "Export");
    const dialog = await screen.findByRole("dialog", { name: "Export 🇳🇱 nl-ams-03" });
    expect(within(dialog).getByLabelText("vless:// link")).toHaveValue(vlessUri(ALL_NODES[0]!));
    expect(JSON.parse((within(dialog).getByLabelText("JSON · full node") as HTMLTextAreaElement).value)).toEqual(ALL_NODES[0]);
    await userEvent.click(within(dialog).getByRole("button", { name: "Copy link" }));
    expect(writeText).toHaveBeenCalledWith(vlessUri(ALL_NODES[0]!));
    await waitFor(() => expect(success).toHaveBeenCalledWith("copied", { duration: 8000 }));
    expect(within(within(dialog).getByRole("button", { name: "Copy link" })).getByText("Copied")).toHaveAttribute("aria-live", "polite");
  });

  it("a copy the browser refuses says so", async () => {
    await openList("/nodes");
    writeText.mockRejectedValue(new DOMException("denied", "NotAllowedError"));
    const error = vi.spyOn(toast, "error");
    await fromMenu("de-fra-01", "Export");
    const dialog = await screen.findByRole("dialog", { name: /Export .*de-fra-01/ });
    await userEvent.click(within(dialog).getByRole("button", { name: "Copy JSON" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("copy failed", { duration: 20000 }));
  });
});
