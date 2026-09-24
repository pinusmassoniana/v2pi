import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type Network, type Settings, type Status } from "../../api/client";
import { CONNECTION_BUSY, SETTINGS_CONNECTION_WRITE, SETTINGS_WRITE, isConnectionBusy } from "../../api/invalidation";
import { keys } from "../../api/keys";
import { settleConfirm } from "../../components/confirm";
import { GATEWAY_NETWORK, SETTINGS, STATUS, holdConnectionWrite, holdWrite, mockApi, mockGateway, networkAfter } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";

afterEach(() => {
  act(() => settleConfirm(false));
  vi.useRealTimers();
});

async function openNetwork(options: { network?: Network; status?: Status; settings?: Partial<Settings> } = {}) {
  const api$ = mockGateway(mockApi());
  if (options.network) api$.getNetwork.mockResolvedValue(options.network);
  if (options.status) api$.getStatus.mockResolvedValue(options.status);
  api$.getSettings.mockResolvedValue({ ...SETTINGS, ...options.settings });
  const view = renderApp("/gateway/network");
  await screen.findByRole("region", { name: "Gateway Segment" });
  await waitFor(() => expect(view.client.getQueryData(keys.status)).toBeDefined());
  return { api$, ...view };
}

const region = (name: string) => screen.getByRole("region", { name });
const field = (label: string) => screen.getByLabelText(label, { selector: "input" });
const segmentApply = () => within(region("Gateway Segment")).getByRole("button", { name: /^(Apply to host|Applying…)$/ });
const footerStatus = () => within(region("Gateway Segment")).getByRole("status");

async function fill(input: HTMLElement, value: string) {
  await userEvent.clear(input);
  await userEvent.click(input);
  await userEvent.paste(value);
}

async function answer(label: string) {
  const dialog = await screen.findByRole("dialog", { name: "Confirm" });
  await userEvent.click(within(dialog).getByRole("button", { name: label }));
}

describe("Network › Apply to host (W4)", () => {
  it("off while nothing changed; on after an edit; off again while a field is invalid", async () => {
    await openNetwork();
    expect(segmentApply()).toBeDisabled();
    expect(within(region("Kill-switch")).queryByRole("button", { name: "Apply to host" })).toBeNull();
    await fill(field("DHCP lease"), "24h");
    await waitFor(() => expect(segmentApply()).toBeEnabled());
    expect(within(region("Kill-switch")).getByRole("button", { name: "Apply to host" })).toBeEnabled();
    await fill(field("DHCP lease"), "24 hours");
    await waitFor(() => expect(segmentApply()).toBeDisabled());
  });

  it("an invalid DHCP pool blocks Apply and says so in the footer's status line", async () => {
    await openNetwork();
    expect(footerStatus()).toBeEmptyDOMElement();
    await fill(field("DHCP range end"), "192.168.50.80");
    await waitFor(() => expect(footerStatus()).toHaveTextContent("✕ DHCP pool invalidfix it to apply"));
    expect(segmentApply()).toBeDisabled();
    await fill(field("DHCP range end"), "192.168.50.180");
    await waitFor(() => expect(segmentApply()).toBeEnabled());
    expect(footerStatus()).toBeEmptyDOMElement();
  });

  it("sends the whole form, then shows what the gateway saved and follows it again", async () => {
    const { api$ } = await openNetwork({ status: { ...STATUS, active_node_id: null } });
    const success = vi.spyOn(toast, "success");
    await fill(field("DHCP range end"), "192.168.50.150");
    await userEvent.click(within(region("LAN access & IPv6")).getByRole("switch", { name: "LAN access" }));
    await waitFor(() => expect(segmentApply()).toBeEnabled());
    await userEvent.click(segmentApply());
    await waitFor(() => expect(api$.putNetwork).toHaveBeenCalledTimes(1));
    expect(api$.putNetwork).toHaveBeenCalledWith({
      segment_iface: "eth0.2", segment_ip: "192.168.50.1", segment_ip6: "2001:db8:5a:2::/64", dhcp_start: "192.168.50.100", dhcp_end: "192.168.50.150",
      dhcp_lease: "12h", client_dns: "192.168.50.1", client_dns6: "2606:4700:4700::1111", kill_switch_enabled: true, lan_access_enabled: false, ipv6_enabled: true,
    });
    expect(screen.queryByRole("dialog", { name: "Confirm" })).toBeNull();
    await waitFor(() => expect(success).toHaveBeenCalledWith("saved · network + DHCP applied to host", { duration: 8000 }));
    await waitFor(() => expect(region("Gateway Segment")).not.toHaveTextContent("unsaved changes"));
    expect(field("DHCP range end")).toHaveValue("192.168.50.150");
    expect(segmentApply()).toBeDisabled();
  });

  it("while it runs: the fields lock, a bar and an m:ss clock show, the button reads Applying…", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    const { api$ } = await openNetwork({ status: { ...STATUS, active_node_id: null } });
    let finish: (network: Network) => void = () => {};
    api$.putNetwork.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    await fill(field("DHCP lease"), "24h");
    await waitFor(() => expect(segmentApply()).toBeEnabled());
    await userEvent.click(segmentApply());
    const bar = await within(region("Gateway Segment")).findByRole("progressbar", { name: "Applying to host" });
    expect(footerStatus()).toContainElement(bar);
    expect(segmentApply()).toHaveTextContent("Applying…");
    expect(segmentApply()).toBeDisabled();
    expect(field("DHCP lease")).toBeDisabled();
    expect(within(region("Kill-switch")).getByRole("switch", { name: "Fail-closed kill-switch" })).toBeDisabled();
    expect(within(region("Gateway Segment")).getByRole("button", { name: "Discard" })).toBeDisabled();
    expect(footerStatus()).toHaveTextContent("0:00");
    await act(() => vi.advanceTimersByTimeAsync(34_000));
    expect(footerStatus()).toHaveTextContent("0:34");
    await act(async () => finish(networkAfter({ dhcp_lease: "24h" })));
    await waitFor(() => expect(segmentApply()).toHaveTextContent("Apply to host"));
    expect(field("DHCP lease")).toBeEnabled();
  });

  it("asks before restarting the tunnel only when connected and the interface, the IP or IPv6 changed; Cancel sends nothing", async () => {
    const { api$ } = await openNetwork();
    const success = vi.spyOn(toast, "success");
    await userEvent.click(within(region("LAN access & IPv6")).getByRole("switch", { name: "IPv6 (tunnel)" }));
    await waitFor(() => expect(segmentApply()).toBeEnabled());
    await userEvent.click(segmentApply());
    const dialog = await screen.findByRole("dialog", { name: "Confirm" });
    expect(dialog).toHaveTextContent("Apply and restart the tunnel? Devices may drop briefly.");
    expect(within(dialog).getByRole("button", { name: "Apply to host" })).not.toHaveClass("text-bad");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(api$.putNetwork).not.toHaveBeenCalled();

    await userEvent.click(segmentApply());
    await answer("Apply to host");
    await waitFor(() => expect(api$.putNetwork).toHaveBeenCalledWith(expect.objectContaining({ ipv6_enabled: false })));
    await waitFor(() => expect(success).toHaveBeenCalledWith("saved · network + DHCP applied to host · tunnel restarted for IPv6", { duration: 8000 }));
  });

  it("asks nothing for other changes while connected, nor for an interface change with no tunnel up", async () => {
    const { api$, client } = await openNetwork();
    await fill(field("DHCP lease"), "24h");
    await waitFor(() => expect(segmentApply()).toBeEnabled());
    await userEvent.click(segmentApply());
    await waitFor(() => expect(api$.putNetwork).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("dialog", { name: "Confirm" })).toBeNull();
    await waitFor(() => expect(segmentApply()).toBeDisabled());

    api$.getStatus.mockResolvedValue({ ...STATUS, active_node_id: null });
    await act(() => client.refetchQueries({ queryKey: keys.status }));
    await fill(field("Segment interface"), "eth0.3");
    await waitFor(() => expect(segmentApply()).toBeEnabled());
    await userEvent.click(segmentApply());
    await waitFor(() => expect(api$.putNetwork).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("dialog", { name: "Confirm" })).toBeNull();
  });

  it("with the status not known, it asks as if the tunnel were up", async () => {
    const { api$, client } = await openNetwork();
    api$.getStatus.mockRejectedValue(new ApiError(0, "network error"));
    await act(() => client.refetchQueries({ queryKey: keys.status }).catch(() => {}));
    await fill(field("Gateway IPv4 address"), "192.168.60.1");
    await fill(field("DHCP range start"), "192.168.60.100");
    await fill(field("DHCP range end"), "192.168.60.200");
    await fill(field("Client DNS"), "192.168.60.1");
    await waitFor(() => expect(segmentApply()).toBeEnabled());
    await userEvent.click(segmentApply());
    expect(await screen.findByRole("dialog", { name: "Confirm" })).toHaveTextContent("Apply and restart the tunnel?");
  });

  it("a connection write that started while the question was open: nothing is sent, and it says why", async () => {
    const { api$, client } = await openNetwork();
    const error = vi.spyOn(toast, "error");
    await fill(field("Segment interface"), "eth0.3");
    await waitFor(() => expect(segmentApply()).toBeEnabled());
    await userEvent.click(segmentApply());
    await screen.findByRole("dialog", { name: "Confirm" });
    const release = holdConnectionWrite(client);
    await answer("Apply to host");
    await waitFor(() => expect(error).toHaveBeenCalledWith(CONNECTION_BUSY, { duration: 20000 }));
    expect(api$.putNetwork).not.toHaveBeenCalled();
    expect(segmentApply()).toBeDisabled();   // waits for the other write
    await release();
    await waitFor(() => expect(segmentApply()).toBeEnabled());
    expect(isConnectionBusy(client)).toBe(false);
  });

  it("a 422 on a field goes under that field; one for the whole form goes in the footer; the edits stay", async () => {
    const { api$ } = await openNetwork({ status: { ...STATUS, active_node_id: null } });
    api$.putNetwork.mockRejectedValueOnce(new ApiError(422, "segment_ip: 192.168.1.5 is in the management network (192.168.1.0/24, which holds mgmt_ip 192.168.1.120) — the segment, its DHCP pool and the LAN-access NAT would overlap the network this panel is reached on. Give the segment its own /24"));
    await fill(field("Gateway IPv4 address"), "192.168.1.5");
    await fill(field("DHCP range start"), "192.168.1.100");
    await fill(field("DHCP range end"), "192.168.1.200");
    await waitFor(() => expect(segmentApply()).toBeEnabled());
    await userEvent.click(segmentApply());
    expect(await screen.findByText(/^segment_ip: 192\.168\.1\.5 is in the management network/)).toBeInTheDocument();
    expect(field("Gateway IPv4 address")).toHaveAttribute("aria-invalid", "true");
    expect(field("Gateway IPv4 address")).toHaveValue("192.168.1.5");

    const joined = "segment_iface: 'eth0' is also the management interface (mgmt_iface); client_dns: 10.0.0.9 is inside the segment network";
    api$.putNetwork.mockRejectedValueOnce(new ApiError(422, joined));
    await fill(field("Gateway IPv4 address"), "192.168.1.6");
    await waitFor(() => expect(segmentApply()).toBeEnabled());
    await userEvent.click(segmentApply());
    await waitFor(() => expect(footerStatus()).toHaveTextContent(joined));
    expect(region("Gateway Segment")).toHaveTextContent("● unsaved changes");
  });

  it("a 502 kept the previous settings: it says so, keeps the edits and re-reads network and status", async () => {
    const { api$ } = await openNetwork({ status: { ...STATUS, active_node_id: null } });
    const error = vi.spyOn(toast, "error");
    api$.putNetwork.mockRejectedValue(new ApiError(502, "host provisioning failed: ip link: no such device"));
    await fill(field("Segment interface"), "eth9");
    await waitFor(() => expect(segmentApply()).toBeEnabled());
    const reads = { network: api$.getNetwork.mock.calls.length, status: api$.getStatus.mock.calls.length };
    await userEvent.click(segmentApply());
    await waitFor(() => expect(error).toHaveBeenCalledWith("not applied — the gateway kept its previous network settings: host provisioning failed: ip link: no such device", { duration: 20000 }));
    expect(field("Segment interface")).toHaveValue("eth9");
    await waitFor(() => expect(api$.getNetwork.mock.calls.length).toBeGreaterThan(reads.network));
    expect(api$.getStatus.mock.calls.length).toBeGreaterThan(reads.status);
    expect(field("Segment interface")).toHaveValue("eth9");
  });

  it("a 502 whose restore reported problems stays until dismissed", async () => {
    const { api$ } = await openNetwork({ status: { ...STATUS, active_node_id: null } });
    const error = vi.spyOn(toast, "error");
    api$.putNetwork.mockRejectedValue(new ApiError(502, "network apply failed; recovery: fail-closed recovery failed: nft: busy"));
    await fill(field("Segment interface"), "eth9");
    await waitFor(() => expect(segmentApply()).toBeEnabled());
    await userEvent.click(segmentApply());
    await waitFor(() => expect(error).toHaveBeenCalledWith(
      "not applied, and restoring the previous state reported problems: network apply failed; recovery: fail-closed recovery failed: nft: busy",
      { duration: Infinity, closeButton: true },
    ));
  });

  it("no answer: an amber note that it may still be applying, the edits kept, and a re-read", async () => {
    const { api$ } = await openNetwork({ status: { ...STATUS, active_node_id: null } });
    const warning = vi.spyOn(toast, "warning");
    api$.putNetwork.mockRejectedValue(new ApiError(0, "request timed out"));
    await fill(field("DHCP lease"), "24h");
    await waitFor(() => expect(segmentApply()).toBeEnabled());
    const reads = api$.getNetwork.mock.calls.length;
    await userEvent.click(segmentApply());
    await waitFor(() => expect(warning).toHaveBeenCalledWith("no answer yet — the gateway may still be applying; reloading", { duration: 20000 }));
    await waitFor(() => expect(api$.getNetwork.mock.calls.length).toBeGreaterThan(reads));
    expect(field("DHCP lease")).toHaveValue("24h");
  });

  it("a disarm applied from the kill-switch card: the pill follows what the gateway saved", async () => {
    const { api$ } = await openNetwork({ status: { ...STATUS, active_node_id: null } });
    const saved = networkAfter({ kill_switch_enabled: false });
    api$.putNetwork.mockResolvedValue(saved);
    const kill = region("Kill-switch");
    await userEvent.click(within(kill).getByRole("switch", { name: "Fail-closed kill-switch" }));
    await answer("Disarm");
    await userEvent.click(await within(kill).findByRole("button", { name: "Apply to host" }));
    await waitFor(() => expect(api$.putNetwork).toHaveBeenCalledWith(expect.objectContaining({ kill_switch_enabled: false })));
    api$.getNetwork.mockResolvedValue(saved);
    expect(await within(kill).findByText("OPEN")).toBeInTheDocument();
    expect(within(kill).getByRole("status")).toBeEmptyDOMElement();
  });
});

describe("Network › Gateway DNS (G1)", () => {
  const dns = () => within(region("Gateway DNS")).getByRole("switch", { name: "Gateway DNS" });

  it("the switch from the settings, the chip, and the hint while client DNS is the gateway", async () => {
    await openNetwork();
    await waitFor(() => expect(dns()).toBeEnabled());
    expect(dns()).not.toBeChecked();
    expect(region("Gateway DNS")).toHaveTextContent("applies live when a node is connected");
    expect(region("Gateway DNS")).toHaveTextContent("Resolve segment DNS in the gateway over DoHfor nodes that don't relay UDP · saved as soon as you flip it");
    expect(region("Gateway DNS")).toHaveTextContent("Client DNS is the gateway itself (192.168.50.1), a private destination — the intercept does not see client DNS.");
  });

  it("no hint while client DNS is elsewhere", async () => {
    await openNetwork({ network: { ...GATEWAY_NETWORK, segment: { ...GATEWAY_NETWORK.segment, client_dns: "1.1.1.1" } } });
    await waitFor(() => expect(dns()).toBeEnabled());
    expect(region("Gateway DNS")).not.toHaveTextContent("Client DNS is the gateway itself");
  });

  it("flips at once and says it reached the live tunnel; with no active node it applies on the next Connect", async () => {
    const { api$, client } = await openNetwork();
    const success = vi.spyOn(toast, "success");
    await waitFor(() => expect(dns()).toBeEnabled());
    let finish: (settings: Settings) => void = () => {};
    api$.putSettings.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    await userEvent.click(dns());
    expect(dns()).toBeChecked();
    expect(api$.putSettings).toHaveBeenCalledWith({ dns_intercept: true });
    expect(isConnectionBusy(client)).toBe(true);
    api$.getSettings.mockResolvedValue({ ...SETTINGS, dns_intercept: true });   // the gateway holds it now
    await act(async () => finish({ ...SETTINGS, dns_intercept: true }));
    await waitFor(() => expect(success).toHaveBeenCalledWith("gateway DNS on · applied to the live tunnel", { duration: 8000 }));

    await waitFor(() => expect(dns()).toBeEnabled());
    expect(dns()).toBeChecked();
    api$.getSettings.mockResolvedValue(SETTINGS);
    await userEvent.click(dns());
    await waitFor(() => expect(success).toHaveBeenCalledWith("gateway DNS off · applied to the live tunnel", { duration: 8000 }));

    api$.getStatus.mockResolvedValue({ ...STATUS, active_node_id: null });
    await act(() => client.refetchQueries({ queryKey: keys.status }));
    await waitFor(() => expect(dns()).toBeEnabled());
    await userEvent.click(dns());
    await waitFor(() => expect(success).toHaveBeenCalledWith("saved — applies on next Connect", { duration: 8000 }));
  });

  it("while the status poll is failing it does not claim the flip reached the live tunnel", async () => {
    const { api$, client } = await openNetwork();
    const success = vi.spyOn(toast, "success");
    await waitFor(() => expect(dns()).toBeEnabled());
    api$.getStatus.mockRejectedValue(new ApiError(0, "network error"));   // the cache still holds an active node
    await act(() => client.refetchQueries({ queryKey: keys.status }));

    await userEvent.click(dns());
    await answer("Continue");   // a status that is not known counts as stopped, so it asks first
    await waitFor(() => expect(api$.putSettings).toHaveBeenCalledWith({ dns_intercept: true }));
    await waitFor(() => expect(success).toHaveBeenCalledWith("saved — applies on next Connect", { duration: 8000 }));
  });

  it("a 502 puts the switch back and says nothing was saved; no answer warns and re-reads", async () => {
    const { api$ } = await openNetwork();
    const error = vi.spyOn(toast, "error");
    const warning = vi.spyOn(toast, "warning");
    await waitFor(() => expect(dns()).toBeEnabled());
    api$.putSettings.mockRejectedValueOnce(new ApiError(502, "doh url must be https"));
    await userEvent.click(dns());
    await waitFor(() => expect(error).toHaveBeenCalledWith("not saved — applying to the tunnel failed: doh url must be https", { duration: 20000 }));
    expect(dns()).not.toBeChecked();

    const reads = api$.getSettings.mock.calls.length;
    api$.putSettings.mockRejectedValueOnce(new ApiError(0, "request timed out"));
    api$.getSettings.mockResolvedValue({ ...SETTINGS, dns_intercept: true });   // it landed after all
    await waitFor(() => expect(dns()).toBeEnabled());
    await userEvent.click(dns());
    await waitFor(() => expect(warning).toHaveBeenCalledWith("no answer yet — the gateway may still be applying; reloading", { duration: 20000 }));
    await waitFor(() => expect(api$.getSettings.mock.calls.length).toBeGreaterThan(reads));
    await waitFor(() => expect(dns()).toBeChecked());
  });

  it("with xray stopped and a node selected it asks before starting the tunnel; Cancel sends nothing", async () => {
    const { api$ } = await openNetwork({ status: { ...STATUS, running: false, xray_state: "stopped" } });
    await waitFor(() => expect(dns()).toBeEnabled());
    await userEvent.click(dns());
    const dialog = await screen.findByRole("dialog", { name: "Confirm" });
    expect(dialog).toHaveTextContent("This starts the tunnel again. Continue?");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(api$.putSettings).not.toHaveBeenCalled();
    expect(dns()).not.toBeChecked();
    await userEvent.click(dns());
    await answer("Continue");
    await waitFor(() => expect(api$.putSettings).toHaveBeenCalledWith({ dns_intercept: true }));
  });

  it("waits while any settings write or connection write runs", async () => {
    const { client } = await openNetwork();
    await waitFor(() => expect(dns()).toBeEnabled());
    for (const hold of [() => holdWrite(client, SETTINGS_WRITE), () => holdWrite(client, SETTINGS_CONNECTION_WRITE), () => holdConnectionWrite(client)]) {
      const release = hold();
      await waitFor(() => expect(dns()).toBeDisabled());
      await release();
      await waitFor(() => expect(dns()).toBeEnabled());
    }
  });

  it("its own fallback when the settings did not load", async () => {
    const api$ = mockGateway(mockApi());
    api$.getSettings.mockRejectedValue(new ApiError(500, "boom"));
    renderApp("/gateway/network");
    expect(await within(await screen.findByRole("region", { name: "Gateway DNS" })).findByText("Gateway DNS did not load", {}, { timeout: 3000 })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Gateway Segment" })).toBeInTheDocument();
  });

  it("a 422 that lands after the screen was left is still said", async () => {
    const { api$, router } = await openNetwork({ status: { ...STATUS, active_node_id: null } });
    const failed = vi.spyOn(toast, "error");
    let refuse!: (error: Error) => void;
    api$.putNetwork.mockImplementationOnce(() => new Promise((_, reject) => { refuse = reject; }));
    await fill(field("DHCP range end"), "192.168.50.150");
    await waitFor(() => expect(segmentApply()).toBeEnabled());
    await userEvent.click(segmentApply());
    await waitFor(() => expect(api$.putNetwork).toHaveBeenCalled());

    act(() => { void router.navigate({ to: "/" }); });
    await answer("Discard");
    // Gone for real, not just navigated: the old screen stays mounted until the next one has loaded.
    await waitFor(() => expect(screen.queryByRole("region", { name: "Gateway Segment" })).toBeNull());
    await act(async () => refuse(new ApiError(422, "segment_ip: not a usable address")));
    await waitFor(() => expect(failed).toHaveBeenCalledWith("segment_ip: not a usable address", expect.anything()));
  });
});
