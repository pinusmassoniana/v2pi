import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { ApiError, type Network } from "../../api/client";
import { keys } from "../../api/keys";
import { hasUnsavedEdits } from "../../app/guard";
import { settleConfirm } from "../../components/confirm";
import { GATEWAY_NETWORK, mockApi, mockGateway } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";

afterEach(() => act(() => settleConfirm(false)));

const withStatus = (patch: Partial<Network["status"]>): Network => ({ ...GATEWAY_NETWORK, status: { ...GATEWAY_NETWORK.status, ...patch } });

async function openNetwork(network: Network = GATEWAY_NETWORK) {
  const api$ = mockGateway(mockApi());
  api$.getNetwork.mockResolvedValue(network);
  const view = renderApp("/gateway/network");
  await screen.findByRole("region", { name: "Gateway Segment" });
  return { api$, ...view };
}

const region = (name: string) => screen.getByRole("region", { name });
const field = (label: string) => screen.getByLabelText(label, { selector: "input" });

/** Replace a field's value in one change, as a paste does: typing long values key by key is slow. */
async function fill(input: HTMLElement, value: string) {
  await userEvent.clear(input);
  await userEvent.click(input);
  await userEvent.paste(value);
}

describe("Network › segment, options, leases and checklist", () => {
  it("shows the saved segment, the options, the kill-switch as the host holds it, the leases and the router steps", async () => {
    await openNetwork();
    expect(field("Segment interface")).toHaveValue("eth0.2");
    expect(field("Gateway IPv4 address")).toHaveValue("192.168.50.1");
    expect(field("DHCP range start")).toHaveValue("192.168.50.100");
    expect(field("DHCP range end")).toHaveValue("192.168.50.200");
    expect(field("Client DNS")).toHaveValue("192.168.50.1");
    expect(field("DHCP lease")).toHaveValue("12h");
    expect(region("Gateway Segment")).toHaveTextContent("nftables tproxy + policy routing");
    expect(region("Gateway Segment")).toHaveTextContent("segment 192.168.50.0/24");
    expect(region("Gateway Segment")).toHaveTextContent("= the gateway IP");
    expect(region("Gateway Segment")).not.toHaveTextContent("unsaved changes");

    const options = region("LAN access & IPv6");
    expect(within(options).getByRole("switch", { name: "LAN access" })).toBeChecked();
    expect(within(options).getByRole("switch", { name: "IPv6 (tunnel)" })).toBeChecked();
    expect(options).toHaveTextContent("Flipping it while the tunnel runs restarts xray.");
    expect(within(within(options).getByRole("group", { name: "Segment IPv6 /64" })).getByRole("radio", { name: "static" })).toBeChecked();
    expect(within(options).getByLabelText("Static /64")).toHaveValue("2001:db8:5a:2::/64");
    expect(within(options).getByLabelText("Client DNS (v6)")).toHaveValue("2606:4700:4700::1111");
    expect(options).toHaveTextContent("prefix sourcestatic");
    expect(options).toHaveTextContent("v6 uplink✓ up");

    const kill = region("Kill-switch");
    expect(within(kill).getByText("ARMED")).toBeInTheDocument();
    expect(kill).toHaveTextContent("fail-closed · traffic dropped if no healthy upstream");
    expect(within(kill).getByRole("switch", { name: "Fail-closed kill-switch" })).toBeChecked();
    expect(kill).not.toHaveTextContent("Apply to host to take effect.");

    const leases = region("DHCP leases");
    expect(leases).toHaveTextContent("7 active");
    expect(leases).toHaveTextContent("live");
    expect(within(leases).getAllByRole("listitem").map((row) => row.textContent)).toEqual([
      "192.168.50.101iphone-anna11h left", "192.168.50.104macbook-pro9h left", "192.168.50.112—47m left", "192.168.50.118pixel-86h left",
      "192.168.50.123appletvno expiry", "192.168.50.140thinkpad-work3h left", "192.168.50.176ipad38m left",
    ]);

    const checklist = region("Router checklist");
    expect(checklist).toHaveTextContent("the one box v2pi never touches");
    expect(checklist).toHaveTextContent("from the saved plan · changes after Apply");
    const steps = within(checklist).getByRole("list");
    expect(steps.tagName).toBe("OL");
    expect(within(steps).getAllByRole("listitem")).toHaveLength(6);
    expect(within(steps).getAllByRole("listitem")[0]).toHaveTextContent("1Create VLAN 2Add VLAN 2 on the router");
    expect(within(checklist).queryByRole("checkbox")).toBeNull();
    expect(screen.queryByRole("status", { name: /changed on the gateway/i })).toBeNull();
  });

  it("no leases and no router steps say so; unknown readouts read unknown", async () => {
    await openNetwork({ ...withStatus({ clients: [], dhcp_clients: 0, uplink6: null, ipv6_prefix_source: null }), recommendations: [] });
    expect(region("DHCP leases")).toHaveTextContent("0 active");
    expect(region("DHCP leases")).toHaveTextContent("No active leases.");
    expect(region("Router checklist")).toHaveTextContent("No router actions outstanding.");
    expect(region("LAN access & IPv6")).toHaveTextContent("prefix sourceunknown");
    expect(region("LAN access & IPv6")).toHaveTextContent("v6 uplinkunknown");
  });

  it("a load that failed is an error with Retry; a refresh that failed keeps the data and says so", async () => {
    const api$ = mockGateway(mockApi());
    api$.getNetwork.mockRejectedValue(new ApiError(500, "boom"));
    const { client } = renderApp("/gateway/network");
    expect(await screen.findByText("Network did not load", {}, { timeout: 3000 })).toBeInTheDocument();
    api$.getNetwork.mockResolvedValue(GATEWAY_NETWORK);
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByRole("region", { name: "Gateway Segment" });
    api$.getNetwork.mockRejectedValue(new ApiError(0, "network error"));
    await act(() => client.refetchQueries({ queryKey: keys.network }).catch(() => {}));
    expect(await screen.findByText("Network did not refresh — showing the last data", {}, { timeout: 3000 })).toBeInTheDocument();
    expect(field("Segment interface")).toHaveValue("eth0.2");
  });
});

describe("Network › host warnings (W1)", () => {
  it("each condition the host reports, in order, with enforcement's error beneath; none can be dismissed", async () => {
    await openNetwork(withStatus({
      wan_blocked: true, enforcement_status: "error", enforcement_error: "nft: Could not process rule", enforcement_warning: "LAN access chain not applied: iptables: No chain/target/match by that name.", foreign_ra: true,
    }));
    const banners = screen.getAllByRole("group").filter((group) => group.hasAttribute("data-tone"));
    expect(banners.map((banner) => banner.getAttribute("aria-label"))).toEqual([
      "WAN blocked", "Host enforcement is error; leak protection is not confirmed.", "Applied with a warning", "Another router is advertising IPv6 on the client VLAN",
    ]);
    expect(banners.map((banner) => banner.dataset.tone)).toEqual(["warn", "bad", "warn", "bad"]);
    expect(banners[0]).toHaveTextContent("WAN blocked — kill-switch holding traffic (tunnel down). No leak.");
    expect(banners[1]).toHaveTextContent("nft: Could not process rule");
    expect(banners[2]).toHaveTextContent("Applied with a warning — LAN access chain not applied: iptables: No chain/target/match by that name.");
    expect(banners[3]).toHaveTextContent("— clients will leak. Disable RA for this VLAN on your router.");
    expect(screen.getByRole("alert", { name: "Host enforcement is error; leak protection is not confirmed." })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Dismiss/ })).toBeNull();
    // the kill-switch is configured but not confirmed
    expect(within(region("Kill-switch")).getByText("UNKNOWN")).toBeInTheDocument();
    expect(region("Kill-switch")).toHaveTextContent("configured, but host enforcement is not confirmed");
  });
});

describe("Network › form rules", () => {
  it("field errors in the backend's words, tied to their fields", async () => {
    await openNetwork();
    await fill(field("Segment interface"), "eth 0");
    expect(await screen.findByText("segment_iface: must be a plain interface name")).toBeInTheDocument();
    expect(field("Segment interface")).toHaveAttribute("aria-invalid", "true");
    await userEvent.clear(field("DHCP lease"));
    expect(await screen.findByText("dhcp_lease: must not be blank")).toBeInTheDocument();
    expect(region("Gateway Segment")).toHaveTextContent("● unsaved changes");
    expect(hasUnsavedEdits()).toBe(true);
  });

  it("a DHCP pool outside the /24 or backwards is an error under the pool, tied to both of its fields", async () => {
    await openNetwork();
    await fill(field("DHCP range end"), "192.168.50.80");
    const error = await screen.findByText("DHCP pool must sit inside 192.168.50.0/24 with start ≤ end");
    for (const label of ["DHCP range start", "DHCP range end"]) {
      expect(field(label)).toHaveAttribute("aria-describedby", error.id);
      expect(field(label)).toHaveAttribute("aria-invalid", "true");
    }
    await fill(field("DHCP range end"), "192.168.50.100");
    await waitFor(() => expect(screen.queryByText(/DHCP pool must sit inside/)).toBeNull());
  });

  it("IPv6 modes: static takes a /64, auto shows the observed prefix, ULA neither; IPv6 off hides the v6 fields", async () => {
    await openNetwork();
    const options = region("LAN access & IPv6");
    await userEvent.click(within(options).getByRole("radio", { name: "auto" }));
    expect(within(options).queryByLabelText("Static /64")).toBeNull();
    expect(options).toHaveTextContent("IPv6 prefixwaiting for a delegated prefix");
    await userEvent.click(within(options).getByRole("radio", { name: "ULA" }));
    expect(options).not.toHaveTextContent("IPv6 prefix");
    await userEvent.click(within(options).getByRole("radio", { name: "static" }));
    // back in static mode, the prefix it held is still there
    await waitFor(() => expect(within(options).getByLabelText("Static /64")).toHaveValue("2001:db8:5a:2::/64"));
    await fill(within(options).getByLabelText("Static /64"), "2001:db8::/48");
    expect(await within(options).findByText("segment_ip6: must use a /64 prefix")).toBeInTheDocument();
    await fill(within(options).getByLabelText("Static /64"), "2001:db8:5a:2::/64");
    await userEvent.click(within(options).getByRole("switch", { name: "IPv6 (tunnel)" }));
    expect(within(options).queryByLabelText("Client DNS (v6)")).toBeNull();
    expect(within(options).queryByRole("group", { name: "Segment IPv6 /64" })).toBeNull();
  });

  it("with auto observed, the delegated prefix reads as the gateway reports it", async () => {
    await openNetwork({ ...withStatus({ ipv6_prefix: "2001:db8:77:2::/64", ipv6_prefix_source: "pd" }), segment: { ...GATEWAY_NETWORK.segment, ip6: "auto" } });
    expect(region("LAN access & IPv6")).toHaveTextContent("IPv6 prefix2001:db8:77:2::/64");
    expect(region("LAN access & IPv6")).toHaveTextContent("prefix sourcepd");
  });

  it("a v6 field holding an error stays on screen with IPv6 off, so the reason Apply is blocked is never hidden", async () => {
    await openNetwork({ ...GATEWAY_NETWORK, ipv6_enabled: false });
    const options = region("LAN access & IPv6");
    await userEvent.click(within(options).getByRole("switch", { name: "IPv6 (tunnel)" }));
    await waitFor(() => expect(within(options).getByLabelText("Client DNS (v6)")).toHaveValue("2606:4700:4700::1111"));
    await fill(within(options).getByLabelText("Client DNS (v6)"), "1.1.1.1");
    await userEvent.click(within(options).getByRole("switch", { name: "IPv6 (tunnel)" }));
    expect(within(options).getByRole("switch", { name: "IPv6 (tunnel)" })).not.toBeChecked();
    expect(within(options).getByLabelText("Client DNS (v6)")).toHaveValue("1.1.1.1");
    expect(within(options).getByText("client_dns6: must be an IPv6 address")).toBeInTheDocument();
  });
});

describe("Network › kill-switch (W6)", () => {
  it("disarming asks first; Cancel keeps it armed; Disarm stages it while the pill keeps the host's state", async () => {
    await openNetwork();
    const kill = region("Kill-switch");
    const toggle = within(kill).getByRole("switch", { name: "Fail-closed kill-switch" });
    await userEvent.click(toggle);
    const dialog = await screen.findByRole("dialog", { name: "Confirm" });
    expect(dialog).toHaveTextContent("Disarm the fail-closed kill-switch? If the tunnel goes down, client traffic will no longer be dropped — it can leak around the tunnel instead.");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(toggle).toBeChecked();
    expect(region("Gateway Segment")).not.toHaveTextContent("unsaved changes");

    await userEvent.click(toggle);
    await userEvent.click(within(await screen.findByRole("dialog", { name: "Confirm" })).getByRole("button", { name: "Disarm" }));
    await waitFor(() => expect(toggle).not.toBeChecked());
    expect(within(kill).getByText("ARMED")).toBeInTheDocument();
    expect(within(kill).getByRole("status")).toHaveTextContent("will disarm on Apply");
    expect(kill).toHaveTextContent("Apply to host to take effect.");

    await userEvent.click(toggle);   // arming again asks nothing
    expect(screen.queryByRole("dialog", { name: "Confirm" })).toBeNull();
    expect(toggle).toBeChecked();
    expect(within(kill).getByRole("status")).toBeEmptyDOMElement();
  });

  it("an open kill-switch reads OPEN and says clients may leak", async () => {
    await openNetwork({ ...GATEWAY_NETWORK, kill_switch_enabled: false });
    expect(within(region("Kill-switch")).getByText("OPEN")).toBeInTheDocument();
    expect(region("Kill-switch")).toHaveTextContent("⚠ clients may leak around the tunnel");
  });

  it("toggling only the kill-switch still revalidates the rest of the form, so a bad saved v6 field shows itself", async () => {
    await openNetwork({ ...GATEWAY_NETWORK, ipv6_enabled: false, segment: { ...GATEWAY_NETWORK.segment, client_dns6: "not-an-ipv6" } });
    const options = region("LAN access & IPv6");
    expect(within(options).queryByLabelText("Client DNS (v6)")).toBeNull();

    await userEvent.click(within(region("Kill-switch")).getByRole("switch", { name: "Fail-closed kill-switch" }));
    await userEvent.click(within(await screen.findByRole("dialog", { name: "Confirm" })).getByRole("button", { name: "Disarm" }));

    expect(await within(options).findByLabelText("Client DNS (v6)")).toHaveValue("not-an-ipv6");
    expect(within(options).getByText("client_dns6: must be an IPv6 address")).toBeInTheDocument();
  });
});

describe("Network › following the gateway", () => {
  it("follows a poll while nothing is edited; once edited it keeps the edits and says the gateway moved on; Discard loads it", async () => {
    const { api$, client } = await openNetwork();
    api$.getNetwork.mockResolvedValue({ ...GATEWAY_NETWORK, segment: { ...GATEWAY_NETWORK.segment, dhcp_lease: "24h" } });
    await act(() => client.refetchQueries({ queryKey: keys.network }));
    await waitFor(() => expect(field("DHCP lease")).toHaveValue("24h"));

    await fill(field("DHCP range end"), "192.168.50.150");
    api$.getNetwork.mockResolvedValue({ ...GATEWAY_NETWORK, segment: { ...GATEWAY_NETWORK.segment, dhcp_lease: "6h" } });
    await act(() => client.refetchQueries({ queryKey: keys.network }));
    const notice = await screen.findByText("Changed on the gateway since you started editing — Discard to load it");
    expect(notice).toHaveAttribute("role", "status");
    expect(field("DHCP lease")).toHaveValue("24h");
    expect(field("DHCP range end")).toHaveValue("192.168.50.150");

    await userEvent.click(within(region("Gateway Segment")).getByRole("button", { name: "Discard" }));
    expect(screen.queryByRole("dialog", { name: "Confirm" })).toBeNull();
    await waitFor(() => expect(field("DHCP lease")).toHaveValue("6h"));
    expect(field("DHCP range end")).toHaveValue("192.168.50.200");
    expect(screen.queryByText(/Changed on the gateway/)).toBeNull();
    expect(hasUnsavedEdits()).toBe(false);
  });

  it("a poll that brings nothing new says nothing, even while edited", async () => {
    const { client } = await openNetwork();
    await fill(field("DHCP lease"), "6h");
    await act(() => client.refetchQueries({ queryKey: keys.network }));
    await expect(screen.findByText(/Changed on the gateway/, {}, { timeout: 300 })).rejects.toThrow();
  });
});
