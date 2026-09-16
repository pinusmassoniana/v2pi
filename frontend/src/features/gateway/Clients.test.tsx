import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type Rw, type Status } from "../../api/client";
import { CONNECTION_BUSY, RW_WRITE } from "../../api/invalidation";
import { keys } from "../../api/keys";
import { settleConfirm } from "../../components/confirm";
import { RW, RW_CLIENTS, STATUS, holdConnectionWrite, holdWrite, mockApi } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";

afterEach(() => {
  act(() => settleConfirm(false));
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
  Object.defineProperty(globalThis, "ClipboardItem", { configurable: true, writable: true, value: undefined });
});

async function openClients(options: { rw?: Rw; status?: Status } = {}) {
  const api$ = mockApi();
  if (options.rw) api$.getRw.mockResolvedValue(options.rw);
  if (options.status) api$.getStatus.mockResolvedValue(options.status);
  const view = renderApp("/gateway/remote-access");
  await screen.findByRole("table", { name: "Clients" });
  await waitFor(() => expect(view.client.getQueryData(keys.status)).toBeDefined());
  return { api$, ...view };
}

const card = () => screen.getByRole("region", { name: "Clients" });
const table = () => within(card()).getByRole("table", { name: "Clients" });
const row = (name: string) => within(table()).getByRole("row", { name: new RegExp(`^${name}`) });
const STOPPED: Status = { ...STATUS, running: false, xray_state: "stopped" };

async function answer(label: string) {
  const dialog = await screen.findByRole("dialog", { name: "Confirm" });
  await userEvent.click(within(dialog).getByRole("button", { name: label }));
}

describe("Remote access › clients (A6)", () => {
  it("a table of the devices: name, status, masked uuid and the row's actions named with the client", async () => {
    await openClients();
    expect(card()).toHaveTextContent("one per device");
    expect(card()).toHaveTextContent("3 / 16");
    expect(card()).toHaveTextContent("Suspend revokes a device now and keeps its uuid — for a lost phone, where Remove would mean reissuing everything.");
    expect(within(table()).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual(["Name", "Status", "UUID", "Actions"]);
    expect(row("iphone-anna")).toHaveTextContent("active");
    expect(row("ipad")).toHaveTextContent("suspended");
    for (const name of ["Suspend iphone-anna", "Download .conf for iphone-anna", "Copy link for iphone-anna", "Remove iphone-anna", "Resume ipad"]) {
      expect(within(table()).getByRole("button", { name })).toBeEnabled();
    }
    expect(within(row("iphone-anna")).getByText("uuid hidden")).toBeInTheDocument();
    expect(within(card()).getByPlaceholderText("iphone")).toBeInTheDocument();
    expect(card()).toHaveTextContent("Enter submits · 1–40 letters, digits, dot, dash or underscore");
  });

  it("no clients yet says so", async () => {
    const api$ = mockApi();
    api$.getRw.mockResolvedValue({ ...RW, clients: [] });
    renderApp("/gateway/remote-access");
    expect(await within(await screen.findByRole("region", { name: "Clients" })).findByText("No clients yet.")).toBeInTheDocument();
  });

  it("a uuid is nowhere in the page until Reveal; Hide masks it again, and so does leaving the screen", async () => {
    const { router } = await openClients();
    const html = () => document.body.innerHTML;
    for (const client of RW_CLIENTS) expect(html()).not.toContain(client.id);
    const reveal = within(row("iphone-anna")).getByRole("button", { name: "Reveal uuid of iphone-anna" });
    expect(reveal).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(reveal);
    expect(reveal).toHaveAttribute("aria-pressed", "true");
    expect(reveal).toHaveTextContent("Hide");
    expect(row("iphone-anna")).toHaveTextContent(RW_CLIENTS[0]!.id);
    expect(html()).not.toContain(RW_CLIENTS[1]!.id);
    expect(html().split(RW_CLIENTS[0]!.id)).toHaveLength(2);   // once, as text — in no attribute
    await userEvent.click(reveal);
    expect(html()).not.toContain(RW_CLIENTS[0]!.id);

    await userEvent.click(within(row("iphone-anna")).getByRole("button", { name: "Reveal uuid of iphone-anna" }));
    await act(() => router.navigate({ to: "/gateway/network" }));
    await screen.findByRole("region", { name: "Gateway Segment" });
    await act(() => router.navigate({ to: "/gateway/remote-access" }));
    await screen.findByRole("table", { name: "Clients" });
    expect(html()).not.toContain(RW_CLIENTS[0]!.id);
  });

  it("Add: a name the gateway accepts, Enter submits, the field clears; a bad name is refused before anything is sent", async () => {
    const { api$ } = await openClients();
    const success = vi.spyOn(toast, "success");
    const input = within(card()).getByPlaceholderText("iphone");
    await userEvent.type(input, "my phone{Enter}");
    expect(await within(card()).findByText("client name must be 1-40 chars of letters, digits, dot, dash or underscore")).toBeInTheDocument();
    expect(api$.addRwClient).not.toHaveBeenCalled();
    await userEvent.clear(input);
    await userEvent.type(input, "e2e-phone{Enter}");
    await waitFor(() => expect(api$.addRwClient).toHaveBeenCalledWith("e2e-phone"));
    await waitFor(() => expect(success).toHaveBeenCalledWith("added e2e-phone", { duration: 8000 }));
    await waitFor(() => expect(input).toHaveValue(""));
    expect(await within(table()).findByRole("row", { name: /^e2e-phone/ })).toBeInTheDocument();
    expect(card()).toHaveTextContent("4 / 16");
  });

  it("Add: a refusal is shown as the gateway words it; a failed re-apply added nothing", async () => {
    const { api$ } = await openClients();
    const error = vi.spyOn(toast, "error");
    const input = within(card()).getByPlaceholderText("iphone");
    api$.addRwClient.mockRejectedValueOnce(new ApiError(422, "a client named 'ipad' already exists"));
    await userEvent.type(input, "ipad{Enter}");
    await waitFor(() => expect(error).toHaveBeenCalledWith("a client named 'ipad' already exists", { duration: 20000 }));
    expect(input).toHaveValue("ipad");
    api$.addRwClient.mockRejectedValueOnce(new ApiError(502, "xray -test failed"));
    await userEvent.clear(input);
    await userEvent.type(input, "tablet{Enter}");
    await waitFor(() => expect(error).toHaveBeenCalledWith("not added — applying to the tunnel failed: xray -test failed", { duration: 20000 }));
  });

  it("Add and Resume ask before starting a stopped tunnel; Cancel sends nothing", async () => {
    const { api$ } = await openClients({ status: STOPPED });
    await userEvent.type(within(card()).getByPlaceholderText("iphone"), "tablet{Enter}");
    expect(await screen.findByRole("dialog", { name: "Confirm" })).toHaveTextContent("This starts the tunnel again. Continue?");
    await answer("Cancel");
    expect(api$.addRwClient).not.toHaveBeenCalled();

    await userEvent.click(within(table()).getByRole("button", { name: "Resume ipad" }));
    await answer("Continue");
    await waitFor(() => expect(api$.setRwClientEnabled).toHaveBeenCalledWith(RW_CLIENTS[1]!.id, true));

    await userEvent.click(within(table()).getByRole("button", { name: "Suspend iphone-anna" }));
    expect(screen.queryByRole("dialog", { name: "Confirm" })).toBeNull();
    await waitFor(() => expect(api$.setRwClientEnabled).toHaveBeenCalledWith(RW_CLIENTS[0]!.id, false));
  });

  it("Suspend keeps the uuid and says how its revocation went; an outage reads as an error", async () => {
    const { api$ } = await openClients();
    const success = vi.spyOn(toast, "success");
    const error = vi.spyOn(toast, "error");
    await userEvent.click(within(table()).getByRole("button", { name: "Suspend iphone-anna" }));
    await waitFor(() => expect(success).toHaveBeenCalledWith("client suspended — its uuid is kept — live tunnel rebuilt without it", { duration: 8000 }));
    expect(await within(table()).findByRole("button", { name: "Resume iphone-anna" })).toBeInTheDocument();

    api$.setRwClientEnabled.mockResolvedValueOnce({ ...RW, clients: RW_CLIENTS.map((c) => ({ ...c, enabled: false })), revocation: "stopped" });
    await userEvent.click(within(table()).getByRole("button", { name: "Suspend laptop-work" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("client suspended — its uuid is kept — xray stopped — remote access is down for everyone until you reconnect", { duration: 20000 }));
  });

  it("Resume says so; a failed re-apply resumed nothing; a client gone meanwhile is re-read", async () => {
    const { api$ } = await openClients();
    const success = vi.spyOn(toast, "success");
    const error = vi.spyOn(toast, "error");
    api$.setRwClientEnabled.mockRejectedValueOnce(new ApiError(502, "xray -test failed"));
    await userEvent.click(within(table()).getByRole("button", { name: "Resume ipad" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("not resumed — applying to the tunnel failed: xray -test failed", { duration: 20000 }));
    await waitFor(() => expect(within(table()).getByRole("button", { name: "Resume ipad" })).toBeEnabled());

    await userEvent.click(within(table()).getByRole("button", { name: "Resume ipad" }));
    await waitFor(() => expect(success).toHaveBeenCalledWith("client resumed", { duration: 8000 }));
    await waitFor(() => expect(within(table()).getByRole("button", { name: "Suspend iphone-anna" })).toBeEnabled());
    // a suspension commits before it revokes: a 502 there is reported as it came, never as "not suspended"
    api$.setRwClientEnabled.mockRejectedValueOnce(new ApiError(502, "bad gateway"));
    await userEvent.click(within(table()).getByRole("button", { name: "Suspend iphone-anna" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("bad gateway", { duration: 20000 }));

    const reads = api$.getRw.mock.calls.length;
    api$.setRwClientEnabled.mockRejectedValueOnce(new ApiError(404, "client not found"));
    await waitFor(() => expect(within(table()).getByRole("button", { name: "Suspend laptop-work" })).toBeEnabled());
    await userEvent.click(within(table()).getByRole("button", { name: "Suspend laptop-work" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("client not found", { duration: 20000 }));
    await waitFor(() => expect(api$.getRw.mock.calls.length).toBeGreaterThan(reads));
  });

  it("Remove asks first; after it, focus moves to the next row, never to the page", async () => {
    const { api$ } = await openClients();
    const success = vi.spyOn(toast, "success");
    await userEvent.click(within(table()).getByRole("button", { name: "Remove ipad" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm" });
    expect(dialog).toHaveTextContent("Remove ipad? Its link and config stop working immediately.");
    expect(within(dialog).getByRole("button", { name: "Remove" })).toHaveClass("text-bad");
    await answer("Cancel");
    expect(api$.deleteRwClient).not.toHaveBeenCalled();

    await userEvent.click(within(table()).getByRole("button", { name: "Remove ipad" }));
    await answer("Remove");
    await waitFor(() => expect(api$.deleteRwClient).toHaveBeenCalledWith(RW_CLIENTS[1]!.id));
    await waitFor(() => expect(success).toHaveBeenCalledWith("removed ipad — live tunnel rebuilt without it", { duration: 8000 }));
    await waitFor(() => expect(within(table()).getByRole("button", { name: "Suspend laptop-work" })).toHaveFocus());
    expect(within(table()).queryByRole("row", { name: /^ipad/ })).toBeNull();
  });

  it("a removal whose stop could not be confirmed: an error, and the pending banner straight from the reply", async () => {
    const { api$ } = await openClients();
    const error = vi.spyOn(toast, "error");
    api$.deleteRwClient.mockResolvedValueOnce({ ...RW, clients: RW_CLIENTS.slice(0, 2), revocation: "stop-failed", revocation_pending: true });
    await userEvent.click(within(table()).getByRole("button", { name: "Remove laptop-work" }));
    await answer("Remove");
    await waitFor(() => expect(error).toHaveBeenCalledWith(
      "removed laptop-work — SECURITY WARNING: xray's stop could not be confirmed, so the revoked device may still be able to connect. The panel keeps retrying; reboot the gateway if this does not clear.",
      { duration: 20000 },
    ));
    expect(await screen.findByRole("alert", { name: "SECURITY WARNING: xray's stop could not be confirmed, so the revoked device may still be able to connect." })).toBeInTheDocument();
  });

  it("a connection write that started while Remove's question was open: nothing is sent, and it says why", async () => {
    const { api$, client } = await openClients();
    const error = vi.spyOn(toast, "error");
    await userEvent.click(within(table()).getByRole("button", { name: "Remove ipad" }));
    await screen.findByRole("dialog", { name: "Confirm" });
    const release = holdConnectionWrite(client);
    await answer("Remove");
    await waitFor(() => expect(error).toHaveBeenCalledWith(CONNECTION_BUSY, { duration: 20000 }));
    expect(api$.deleteRwClient).not.toHaveBeenCalled();
    await release();
  });

  it("any remote-access write holds every row; another connection write holds the writes but not .conf or Copy link", async () => {
    const { client } = await openClients();
    const buttons = () => within(table()).getAllByRole("button").filter((button) => !button.hasAttribute("aria-pressed"));
    let release = holdWrite(client, RW_WRITE);
    await waitFor(() => expect(buttons().every((button) => (button as HTMLButtonElement).disabled)).toBe(true));
    expect(within(card()).getByRole("button", { name: "Add" })).toBeDisabled();
    await release();
    release = holdConnectionWrite(client);
    await waitFor(() => expect(within(table()).getByRole("button", { name: "Remove ipad" })).toBeDisabled());
    expect(within(table()).getByRole("button", { name: "Download .conf for ipad" })).toBeEnabled();
    expect(within(table()).getByRole("button", { name: "Copy link for ipad" })).toBeEnabled();
    await release();
  });

  it(".conf downloads the gateway's file from memory and says where to import it; a refusal says what to set", async () => {
    const { api$ } = await openClients();
    const success = vi.spyOn(toast, "success");
    const error = vi.spyOn(toast, "error");
    const blobs: Blob[] = [];
    URL.createObjectURL = vi.fn((blob: Blob) => { blobs.push(blob); return "blob:conf"; });
    URL.revokeObjectURL = vi.fn();
    const clicked: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function click(this: HTMLAnchorElement) { clicked.push(this.download); });
    await userEvent.click(within(table()).getByRole("button", { name: "Download .conf for iphone-anna" }));
    await waitFor(() => expect(success).toHaveBeenCalledWith("iphone-anna.conf downloaded — import it in Shadowrocket", { duration: 8000 }));
    expect(api$.rwClientConfig).toHaveBeenCalledWith(RW_CLIENTS[0]!.id);
    expect(clicked).toEqual(["iphone-anna.conf"]);
    expect(await blobs[0]!.text()).toContain("[Proxy]\niphone-anna = vless, vpn.example.net, 8443");

    api$.rwClientConfig.mockRejectedValueOnce(new ApiError(422, "set the external endpoint (DDNS name or WAN IP) first"));
    await userEvent.click(within(table()).getByRole("button", { name: "Download .conf for ipad" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("set the external endpoint (DDNS name or WAN IP) first", { duration: 20000 }));
  });

  it("Copy link writes the link through a ClipboardItem started in the click, never shows it, and works over plain HTTP", async () => {
    const { api$ } = await openClients();
    const success = vi.spyOn(toast, "success");
    const error = vi.spyOn(toast, "error");
    class FakeClipboardItem { constructor(public items: Record<string, Promise<Blob>>) {} }
    const written: FakeClipboardItem[] = [];
    Object.defineProperty(globalThis, "ClipboardItem", { configurable: true, writable: true, value: FakeClipboardItem });
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { write: vi.fn(async (items: FakeClipboardItem[]) => { written.push(...items); }), writeText: vi.fn() } });
    await userEvent.click(within(table()).getByRole("button", { name: "Copy link for iphone-anna" }));
    await waitFor(() => expect(success).toHaveBeenCalledWith("vless:// link copied", { duration: 8000 }));
    const link = await (await written[0]!.items["text/plain"]!).text();
    expect(link).toMatch(/^vless:\/\/3f2a9c1e-7b4d-4e8a-9c21-5d6e7f809a1b@vpn\.example\.net:8443/);
    expect(document.body.innerHTML).not.toContain(link);
    expect(document.body.innerHTML).not.toContain("@vpn.example.net:8443");
    expect(success.mock.calls.flat().join(" ")).not.toContain(RW_CLIENTS[0]!.id);

    Object.defineProperty(globalThis, "ClipboardItem", { configurable: true, writable: true, value: undefined });
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    const copied: string[] = [];
    Object.defineProperty(document, "execCommand", { configurable: true, writable: true, value: vi.fn(() => { copied.push(document.querySelector("textarea")?.value ?? ""); return true; }) });
    await userEvent.click(within(table()).getByRole("button", { name: "Copy link for ipad" }));
    await waitFor(() => expect(copied).toHaveLength(1));
    expect(copied[0]).toMatch(/^vless:\/\/b01c55d2-/);
    expect(document.querySelector("textarea")).toBeNull();

    api$.rwClientLink.mockRejectedValueOnce(new ApiError(422, "set the Reality public key first"));
    await userEvent.click(within(table()).getByRole("button", { name: "Copy link for laptop-work" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("set the Reality public key first", { duration: 20000 }));
    Object.defineProperty(document, "execCommand", { configurable: true, writable: true, value: undefined });
    await userEvent.click(within(table()).getByRole("button", { name: "Copy link for laptop-work" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("copy failed", { duration: 20000 }));
  });
});
