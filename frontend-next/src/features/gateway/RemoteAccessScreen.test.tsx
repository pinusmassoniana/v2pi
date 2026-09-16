import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type Rw, type RwIn, type Status } from "../../api/client";
import { CONNECTION_BUSY } from "../../api/invalidation";
import { keys } from "../../api/keys";
import { settleConfirm } from "../../components/confirm";
import { RW, RW_PENDING, RW_PRIVATE_KEY, RW_PUBLIC_KEY, STATUS, holdConnectionWrite, mockApi } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";

afterEach(() => {
  act(() => settleConfirm(false));
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
});

async function openRemoteAccess(options: { rw?: Rw; status?: Status } = {}) {
  const api$ = mockApi();
  if (options.rw) api$.getRw.mockResolvedValue(options.rw);
  if (options.status) api$.getStatus.mockResolvedValue(options.status);
  const view = renderApp("/gateway/remote-access");
  await screen.findByRole("region", { name: "Remote Access Inbound" });
  await waitFor(() => expect(view.client.getQueryData(keys.status)).toBeDefined());
  return { api$, ...view };
}

const region = (name: string) => screen.getByRole("region", { name });
const inbound = () => region("Remote Access Inbound");
const field = (label: string) => within(inbound()).getByLabelText(label, { selector: "input" });
const saveButton = () => screen.getByRole("button", { name: /^(Save|Saving…)$/ });
const banners = () => screen.queryAllByRole("group").filter((group) => group.hasAttribute("data-tone"));

async function fill(input: HTMLElement, value: string) {
  await userEvent.clear(input);
  if (value === "") return;
  await userEvent.click(input);
  await userEvent.paste(value);
}

async function answer(label: string) {
  const dialog = await screen.findByRole("dialog", { name: "Confirm" });
  await userEvent.click(within(dialog).getByRole("button", { name: label }));
}

describe("Remote access › the inbound, hosts, subnets and checklist", () => {
  it("shows the saved inbound as chips and fields, the hosts, the derived subnets and the router steps with its port", async () => {
    await openRemoteAccess();
    expect(inbound()).toHaveTextContent("VLESS · XTLS-Vision · Reality");
    expect(within(inbound()).getByText("live")).toBeInTheDocument();
    expect(within(inbound()).getByRole("switch", { name: "Accept inbound connections" })).toBeChecked();
    expect(inbound()).toHaveTextContent("private key STORED");
    expect(field("Listen port")).toHaveValue("8443");
    expect(field("External endpoint")).toHaveValue("vpn.example.net");
    expect(field("Reality dest")).toHaveValue("www.microsoft.com:443");
    expect(field("Public key")).toHaveValue(RW_PUBLIC_KEY);
    expect(within(within(inbound()).getByRole("list", { name: "Server names list" })).getAllByRole("listitem").map((chip) => chip.textContent)).toEqual(["www.microsoft.com", "learn.microsoft.com"]);
    expect(within(within(inbound()).getByRole("list", { name: "Short ids list" })).getAllByRole("listitem").map((chip) => chip.textContent)).toEqual(["3a9e", "6ba85179e3d4fc21"]);
    const key = field("Private key");
    expect(key).toHaveAttribute("type", "password");
    expect(key).toHaveAttribute("autocomplete", "off");
    expect(key).toHaveValue("");
    expect(key).toHaveAttribute("placeholder", "•••••••• stored");
    expect(inbound()).toHaveTextContent("stored — leave blank to keep it");
    expect(inbound()).toHaveTextContent("Generate the pair once on the gateway — docker exec v2pi xray x25519 — and paste both halves here.");

    const hosts = region("LAN hosts by name");
    expect(hosts).toHaveTextContent("2 / 32");
    expect(within(hosts).getByLabelText("Host 1 name")).toHaveValue("nas.v2pi");
    expect(within(hosts).getByLabelText("Host 2 IPv4")).toHaveValue("192.168.1.20");
    const subnets = region("Routed subnets");
    expect(within(within(subnets).getByRole("list", { name: "Routed subnets" })).getAllByRole("listitem").map((chip) => chip.textContent)).toEqual(["192.168.1.0/24", "192.168.50.0/24"]);
    expect(within(subnets).getByLabelText("Override")).toHaveValue("");
    const steps = within(region("Router checklist")).getByRole("list");
    expect(steps.tagName).toBe("OL");
    expect(within(steps).getAllByRole("listitem").map((step) => step.textContent)).toEqual([
      "1Port-forward WAN :8443 → this gateway, on every WAN linka failover that swaps providers must not drop the rule.",
      "2DDNS in direct mode.A cloud/proxied DDNS mode forwards HTTP(S) only and will not carry Reality's raw TCP.",
      "3Check :8443 is free on the router itselfits own web UI or remote-access service often sits on 443.",
    ]);
    expect(saveButton()).toBeDisabled();
    expect(screen.getByText("Save sends the whole form — settings, LAN hosts and routed subnets")).toBeInTheDocument();
    expect(banners()).toEqual([]);
  });

  it("a load that failed is an error with Retry", async () => {
    const api$ = mockApi();
    api$.getRw.mockRejectedValue(new ApiError(500, "boom"));
    renderApp("/gateway/remote-access");
    expect(await screen.findByText("Remote access did not load", {}, { timeout: 3000 })).toBeInTheDocument();
  });
});

describe("Remote access › warnings (A1)", () => {
  it("a pending revocation is a security warning with its badge and no dismiss, before malformed state", async () => {
    await openRemoteAccess({ rw: { ...RW_PENDING, state_error: "rw_port must be an integer, got 'x'" } });
    expect(banners().map((banner) => [banner.getAttribute("aria-label"), banner.dataset.tone])).toEqual([
      ["SECURITY WARNING: xray's stop could not be confirmed, so the revoked device may still be able to connect.", "bad"],
      ["Stored settings are malformed and were ignored:", "bad"],
    ]);
    expect(banners()[0]).toHaveTextContent("revocation pending");
    expect(banners()[0]).toHaveTextContent("The panel keeps retrying; reboot the gateway if this does not clear.");
    expect(banners()[1]).toHaveTextContent("Stored settings are malformed and were ignored: rw_port must be an integer, got 'x'. Save this form to overwrite them.");
    expect(screen.queryByRole("button", { name: /^Dismiss/ })).toBeNull();
    expect(within(inbound()).getByRole("switch", { name: "Accept inbound connections" })).toBeEnabled();
  });

  it("at most one of: no stored key, no enabled client, not live", async () => {
    const { api$, client } = await openRemoteAccess({ rw: { ...RW, has_private_key: false, live: false } });
    expect(banners()[0]).toHaveTextContent("Enabled, but no private key is stored (keys are not restored from backups)");
    expect(banners()).toHaveLength(1);
    api$.getRw.mockResolvedValue({ ...RW, clients: RW.clients.map((c) => ({ ...c, enabled: false })), live: false });
    await act(() => client.refetchQueries({ queryKey: keys.rw }));
    await waitFor(() => expect(banners()[0]).toHaveTextContent("Enabled with no clients — nothing is listening."));
    api$.getRw.mockResolvedValue({ ...RW, live: false });
    await act(() => client.refetchQueries({ queryKey: keys.rw }));
    await waitFor(() => expect(banners()[0]).toHaveTextContent("Stored, but not in the running config yet — either there is no active node to rebuild it from"));
    expect(banners()).toHaveLength(1);
  });

  it("server names that miss the dest host: a warning, an amber field and a line saying which host", async () => {
    await openRemoteAccess();
    const names = within(inbound()).getByLabelText("Server names");
    await userEvent.click(within(inbound()).getByRole("button", { name: "Remove server name www.microsoft.com" }));
    expect(await screen.findByRole("group", { name: "Server name does not match dest." })).toHaveTextContent("Reality needs the SNI to be what the dest host actually serves");
    expect(inbound()).toHaveTextContent("dest host is www.microsoft.com — it is not in this list");
    expect(names.parentElement).toHaveClass("border-warn/70");
    await fill(field("Reality dest"), "learn.microsoft.com:443");
    await waitFor(() => expect(screen.queryByRole("group", { name: "Server name does not match dest." })).toBeNull());
  });
});

describe("Remote access › fields", () => {
  it("switching the inbound on needs a key, stored or typed; switching it off never does", async () => {
    await openRemoteAccess({ rw: { ...RW, enabled: false, has_private_key: false } });
    const toggle = within(inbound()).getByRole("switch", { name: "Accept inbound connections" });
    expect(toggle).toBeDisabled();
    expect(inbound()).toHaveTextContent("required before enabling");
    expect(field("Private key")).toHaveAttribute("placeholder", "paste the private key from `xray x25519`");
    await fill(field("Private key"), RW_PRIVATE_KEY);
    await waitFor(() => expect(toggle).toBeEnabled());
    await userEvent.click(toggle);
    expect(toggle).toBeChecked();
  });

  it("an enabled inbound with no stored key can still be switched off", async () => {
    await openRemoteAccess({ rw: { ...RW, has_private_key: false } });
    const toggle = within(inbound()).getByRole("switch", { name: "Accept inbound connections" });
    expect(toggle).toBeEnabled();
    await userEvent.click(toggle);
    expect(toggle).not.toBeChecked();
  });

  it("chips: a comma, Enter or leaving the field adds; × removes; a chip the gateway does not hold is marked", async () => {
    await openRemoteAccess();
    const input = within(inbound()).getByLabelText("Server names");
    const chips = () => within(within(inbound()).getByRole("list", { name: "Server names list" })).getAllByRole("listitem");
    await userEvent.type(input, "cdn.example,");
    expect(chips().map((chip) => chip.textContent)).toEqual(["www.microsoft.com", "learn.microsoft.com", "cdn.example"]);
    await userEvent.type(input, "edge.example{Enter}");
    await userEvent.type(input, "last.example");
    await userEvent.tab();
    expect(chips().map((chip) => chip.textContent)).toEqual(["www.microsoft.com", "learn.microsoft.com", "cdn.example", "edge.example", "last.example"]);
    expect(chips().map((chip) => chip.dataset.unsaved ?? "")).toEqual(["", "", "true", "true", "true"]);
    await userEvent.click(within(inbound()).getByRole("button", { name: "Remove server name cdn.example" }));
    expect(chips()).toHaveLength(4);
    await userEvent.type(input, "bad name,");
    expect(await within(inbound()).findByText("server name must be a host name (letters, digits, dashes and dots), got 'bad name'")).toBeInTheDocument();
  });

  it("Generate adds a fresh short id, marked as not saved, and says Save stores it", async () => {
    const { api$ } = await openRemoteAccess();
    const success = vi.spyOn(toast, "success");
    await userEvent.click(within(inbound()).getByRole("button", { name: "Generate" }));
    await waitFor(() => expect(success).toHaveBeenCalledWith("short id added — save to apply", { duration: 8000 }));
    const chips = within(within(inbound()).getByRole("list", { name: "Short ids list" })).getAllByRole("listitem");
    expect(chips.at(-1)).toHaveTextContent("0123456789abcdef");
    expect(chips.at(-1)).toHaveAttribute("data-unsaved", "true");
    expect(api$.newRwShortId).toHaveBeenCalledTimes(1);
    expect(saveButton()).toBeEnabled();

    const error = vi.spyOn(toast, "error");
    api$.newRwShortId.mockRejectedValue(new ApiError(0, "network error"));
    await userEvent.click(within(inbound()).getByRole("button", { name: "Generate" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("could not generate a short id", { duration: 20000 }));
  });

  it("copies the public key, over plain HTTP too", async () => {
    await openRemoteAccess();
    const success = vi.spyOn(toast, "success");
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    await userEvent.click(within(inbound()).getByRole("button", { name: "Copy public key" }));
    await waitFor(() => expect(success).toHaveBeenCalledWith("public key copied", { duration: 8000 }));
    expect(writeText).toHaveBeenCalledWith(RW_PUBLIC_KEY);

    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    const execCommand = vi.fn(() => true);
    Object.defineProperty(document, "execCommand", { configurable: true, writable: true, value: execCommand });
    await userEvent.click(within(inbound()).getByRole("button", { name: "Copy public key" }));
    await waitFor(() => expect(execCommand).toHaveBeenCalledWith("copy"));
    Object.defineProperty(document, "execCommand", { configurable: true, writable: true, value: undefined });
    const error = vi.spyOn(toast, "error");
    await userEvent.click(within(inbound()).getByRole("button", { name: "Copy public key" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("copy failed", { duration: 20000 }));
  });

  it("hosts: a half-filled row and a duplicate name block Save with their reasons; removing a row keeps focus in the list", async () => {
    const { api$ } = await openRemoteAccess();
    const hosts = region("LAN hosts by name");
    await userEvent.click(within(hosts).getByRole("button", { name: "Add host" }));
    const name3 = within(hosts).getByLabelText("Host 3 name");
    await waitFor(() => expect(name3).toHaveFocus());
    await userEvent.paste("NAS.v2pi");
    expect(await within(hosts).findByText('host "NAS.v2pi" needs both a name and an IP')).toBeInTheDocument();
    await userEvent.click(within(hosts).getByLabelText("Host 3 IPv4"));
    await userEvent.paste("192.168.1.30");
    expect(await within(hosts).findByText("host nas.v2pi is listed twice")).toBeInTheDocument();
    expect(within(hosts).getByLabelText("Host 3 name")).toHaveAttribute("aria-invalid", "true");
    expect(hosts).toHaveTextContent("3 / 32");
    await userEvent.click(saveButton());
    expect(api$.putRw).not.toHaveBeenCalled();

    await userEvent.click(within(hosts).getByRole("button", { name: "Remove host 1" }));
    await waitFor(() => expect(within(hosts).getByLabelText("Host 1 name")).toHaveFocus());
    expect(within(hosts).getByLabelText("Host 1 name")).toHaveValue("printer.v2pi");
    await waitFor(() => expect(within(hosts).queryByText("host nas.v2pi is listed twice")).toBeNull());
    for (const index of [2, 1]) await userEvent.click(within(hosts).getByRole("button", { name: `Remove host ${index}` }));
    await waitFor(() => expect(within(hosts).getByRole("button", { name: "Add host" })).toHaveFocus());
    expect(hosts).toHaveTextContent("0 / 32");
  });
});

describe("Remote access › Save (A4)", () => {
  const FULL: RwIn = {
    enabled: true, port: 8443, dest: "www.microsoft.com:443", server_names: "www.microsoft.com,learn.microsoft.com", short_ids: "3a9e,6ba85179e3d4fc21",
    public_key: RW_PUBLIC_KEY, endpoint: "vpn.example.net", private_key: "", hosts: { "nas.v2pi": "192.168.1.10", "printer.v2pi": "192.168.1.20" }, routed_nets: "",
  };

  it("sends every field of the full replace, with private_key \"\" to keep the stored one, and says the live config took it", async () => {
    const { api$ } = await openRemoteAccess();
    const success = vi.spyOn(toast, "success");
    await fill(within(region("Routed subnets")).getByLabelText("Override"), "10.9.0.0/24");
    await waitFor(() => expect(saveButton()).toBeEnabled());
    await userEvent.click(saveButton());
    await waitFor(() => expect(api$.putRw).toHaveBeenCalledTimes(1));
    expect(api$.putRw.mock.calls[0]![0]).toEqual({ ...FULL, routed_nets: "10.9.0.0/24" });
    await waitFor(() => expect(success).toHaveBeenCalledWith("saved · rebuilt into the live config", { duration: 8000 }));
    await waitFor(() => expect(saveButton()).toBeDisabled());
    expect(screen.queryByText("● unsaved changes")).toBeNull();
  });

  it("with no active node it applies on the next connect; a typed key is sent, then the field goes blank", async () => {
    const { api$, client } = await openRemoteAccess({ status: { ...STATUS, active_node_id: null } });
    const success = vi.spyOn(toast, "success");
    await fill(field("Private key"), RW_PRIVATE_KEY);
    await waitFor(() => expect(saveButton()).toBeEnabled());
    await userEvent.click(saveButton());
    await waitFor(() => expect(api$.putRw).toHaveBeenCalledWith({ ...FULL, private_key: RW_PRIVATE_KEY }));
    await waitFor(() => expect(success).toHaveBeenCalledWith("saved · no active node right now, so it applies on the next connect", { duration: 8000 }));
    await waitFor(() => expect(field("Private key")).toHaveValue(""));
    expect(JSON.stringify(client.getQueryCache().getAll().map((query) => query.state.data))).not.toContain(RW_PRIVATE_KEY);
  });

  it("keeps the private key out of the MutationCache after a save (not just the QueryCache)", async () => {
    const { client } = await openRemoteAccess({ status: { ...STATUS, active_node_id: null } });
    await fill(field("Private key"), RW_PRIVATE_KEY);
    await waitFor(() => expect(saveButton()).toBeEnabled());
    await userEvent.click(saveButton());
    await waitFor(() => expect(field("Private key")).toHaveValue(""));
    const mutations = client.getMutationCache().getAll();
    expect(mutations.length).toBeGreaterThan(0);
    const serialized = JSON.stringify(mutations.map((mutation) => mutation.state.variables));
    expect(serialized).not.toContain(RW_PRIVATE_KEY);
  });

  it("a narrowing save reports how its revocation went; a stop that could not be confirmed is an error with the pending banner", async () => {
    const { api$ } = await openRemoteAccess();
    const success = vi.spyOn(toast, "success");
    const error = vi.spyOn(toast, "error");
    api$.putRw.mockResolvedValueOnce({ ...RW, short_ids: "6ba85179e3d4fc21", revocation: "reapplied" });
    await userEvent.click(within(inbound()).getByRole("button", { name: "Remove short id 3a9e" }));
    await userEvent.click(saveButton());
    await waitFor(() => expect(success).toHaveBeenCalledWith("saved · live tunnel rebuilt with the narrowed settings", { duration: 8000 }));

    api$.putRw.mockResolvedValueOnce({ ...RW, port: 9443, revocation: "stop-failed", revocation_pending: true });
    api$.getRw.mockResolvedValue({ ...RW, port: 9443, revocation_pending: true });
    await fill(field("Listen port"), "9443");
    await waitFor(() => expect(saveButton()).toBeEnabled());
    await userEvent.click(saveButton());
    await waitFor(() => expect(error).toHaveBeenCalledWith(
      "saved · SECURITY WARNING: xray's stop could not be confirmed, so the settings you just narrowed may still be live. The panel keeps retrying; reboot the gateway if this does not clear.",
      { duration: 20000 },
    ));
    expect(await screen.findByRole("alert", { name: "SECURITY WARNING: xray's stop could not be confirmed, so the revoked device may still be able to connect." })).toBeInTheDocument();
  });

  it("with xray stopped and a node selected, a save that may widen asks first; one that provably narrows does not", async () => {
    const { api$ } = await openRemoteAccess({ status: { ...STATUS, running: false, xray_state: "stopped" } });
    await userEvent.type(within(inbound()).getByLabelText("Server names"), "cdn.example,");
    await userEvent.click(saveButton());
    expect(await screen.findByRole("dialog", { name: "Confirm" })).toHaveTextContent("This starts the tunnel again. Continue?");
    await answer("Cancel");
    expect(api$.putRw).not.toHaveBeenCalled();
    await userEvent.click(saveButton());
    await answer("Continue");
    await waitFor(() => expect(api$.putRw).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(saveButton()).toBeDisabled());

    await userEvent.click(within(inbound()).getByRole("button", { name: "Remove short id 3a9e" }));
    await userEvent.click(saveButton());
    await waitFor(() => expect(api$.putRw).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("dialog", { name: "Confirm" })).toBeNull();
  });

  it("a connection write that started while the question was open: nothing is sent, and it says why", async () => {
    const { api$, client } = await openRemoteAccess({ status: { ...STATUS, running: false, xray_state: "stopped" } });
    const error = vi.spyOn(toast, "error");
    await fill(field("External endpoint"), "home.example.org");
    await waitFor(() => expect(saveButton()).toBeEnabled());
    await userEvent.click(saveButton());
    await screen.findByRole("dialog", { name: "Confirm" });
    const release = holdConnectionWrite(client);
    await answer("Continue");
    await waitFor(() => expect(error).toHaveBeenCalledWith(CONNECTION_BUSY, { duration: 20000 }));
    expect(api$.putRw).not.toHaveBeenCalled();
    await release();
  });

  it("a schema refusal lands on its field; one naming no field lands in the toolbar's status line", async () => {
    const { api$ } = await openRemoteAccess();
    api$.putRw.mockRejectedValueOnce(new ApiError(422, "body.dest: Value error, rw_dest port out of range: 0", [
      { loc: ["body", "dest"], msg: "Value error, rw_dest port out of range: 0", type: "value_error", input: "www.microsoft.com:0" },
    ]));
    await fill(field("External endpoint"), "home.example.org");
    await waitFor(() => expect(saveButton()).toBeEnabled());
    await userEvent.click(saveButton());
    expect(await within(inbound()).findByText("rw_dest port out of range: 0")).toBeInTheDocument();
    expect(field("Reality dest")).toHaveAttribute("aria-invalid", "true");
    expect(field("External endpoint")).toHaveValue("home.example.org");

    api$.putRw.mockRejectedValueOnce(new ApiError(422, "set the Reality public key before enabling the inbound"));
    await fill(field("Reality dest"), "www.microsoft.com:443");
    await waitFor(() => expect(saveButton()).toBeEnabled());
    await userEvent.click(saveButton());
    const line = await screen.findByText("set the Reality public key before enabling the inbound");
    expect(line).toHaveAttribute("role", "status");
  });

  it("a refused widening save was rolled back: it says nothing was saved and keeps the edits; no answer warns and re-reads", async () => {
    const { api$ } = await openRemoteAccess();
    const error = vi.spyOn(toast, "error");
    const warning = vi.spyOn(toast, "warning");
    api$.putRw.mockRejectedValueOnce(new ApiError(502, "xray -test failed: bad inbound"));
    await fill(field("External endpoint"), "home.example.org");
    await waitFor(() => expect(saveButton()).toBeEnabled());
    await userEvent.click(saveButton());
    await waitFor(() => expect(error).toHaveBeenCalledWith("not saved — applying to the tunnel failed: xray -test failed: bad inbound", { duration: 20000 }));
    expect(field("External endpoint")).toHaveValue("home.example.org");

    const reads = api$.getRw.mock.calls.length;
    api$.putRw.mockRejectedValueOnce(new ApiError(0, "request timed out"));
    await waitFor(() => expect(saveButton()).toBeEnabled());
    await userEvent.click(saveButton());
    await waitFor(() => expect(warning).toHaveBeenCalledWith("no answer yet — the gateway may still be applying; reloading", { duration: 20000 }));
    await waitFor(() => expect(api$.getRw.mock.calls.length).toBeGreaterThan(reads));
    expect(field("External endpoint")).toHaveValue("home.example.org");
  });

  it("a malformed key never leaves the browser", async () => {
    const { api$ } = await openRemoteAccess();
    await fill(field("Private key"), "not-a-key");
    expect(await within(inbound()).findByText("the Reality private key must be a base64 x25519 key — 43 characters, exactly as `xray x25519` prints it")).toBeInTheDocument();
    await userEvent.click(saveButton());
    expect(api$.putRw).not.toHaveBeenCalled();
    expect(screen.queryByText(/not-a-key/)).toBeNull();
  });
});

describe("Remote access › following the gateway and leaving", () => {
  it("follows a poll while clean; once edited it says the gateway moved on, and Discard loads it and blanks the key", async () => {
    const { api$, client } = await openRemoteAccess();
    api$.getRw.mockResolvedValue({ ...RW, endpoint: "home.example.org" });
    await act(() => client.refetchQueries({ queryKey: keys.rw }));
    await waitFor(() => expect(field("External endpoint")).toHaveValue("home.example.org"));
    await fill(field("Private key"), RW_PRIVATE_KEY);
    api$.getRw.mockResolvedValue({ ...RW, endpoint: "wan.example.org" });
    await act(() => client.refetchQueries({ queryKey: keys.rw }));
    expect(await screen.findByText("Changed on the gateway since you started editing — Discard to load it")).toHaveAttribute("role", "status");
    expect(field("External endpoint")).toHaveValue("home.example.org");
    await userEvent.click(screen.getByRole("button", { name: "Discard" }));
    await waitFor(() => expect(field("External endpoint")).toHaveValue("wan.example.org"));
    expect(field("Private key")).toHaveValue("");
  });

  it("a key typed and left behind is gone when the screen is opened again", async () => {
    const { router } = await openRemoteAccess();
    await fill(field("Private key"), RW_PRIVATE_KEY);
    act(() => void router.navigate({ to: "/gateway/network" }));
    expect(await screen.findByRole("dialog", { name: "Confirm" })).toHaveTextContent("Discard unsaved changes and leave this screen?");
    await answer("Discard");
    await screen.findByRole("region", { name: "Gateway Segment" });
    await act(() => router.navigate({ to: "/gateway/remote-access" }));
    await screen.findByRole("region", { name: "Remote Access Inbound" });
    expect(field("Private key")).toHaveValue("");
  });
});
