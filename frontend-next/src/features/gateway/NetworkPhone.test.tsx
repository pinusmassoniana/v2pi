import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { STATUS, mockApi, mockGateway } from "../../test/fixtures";
import { settleConfirm } from "../../components/confirm";
import { renderApp } from "../../test/renderApp";
import { setViewportWidth } from "../../test/viewport";

afterEach(() => act(() => settleConfirm(false)));

async function openPhone() {
  setViewportWidth(390);
  const api$ = mockGateway(mockApi());
  api$.getStatus.mockResolvedValue({ ...STATUS, active_node_id: null });
  const view = renderApp("/gateway/network");
  await screen.findByRole("region", { name: "Kill-switch" });
  return { api$, ...view };
}

const header = (name: string) => screen.getByRole("button", { name: new RegExp(`^${name}`) });
const section = (name: string) => screen.getByRole("group", { name: new RegExp(`^${name}`) });
const footerButton = (name: string) => screen.queryByRole("button", { name });

async function fill(input: HTMLElement, value: string) {
  await userEvent.clear(input);
  await userEvent.click(input);
  await userEvent.paste(value);
}

describe("Network on a phone", () => {
  it("the kill-switch and the leases as cards first, then collapsed sections with their summaries; no footer while clean", async () => {
    await openPhone();
    const kill = screen.getByRole("region", { name: "Kill-switch" });
    expect(within(kill).getByText("ARMED")).toBeInTheDocument();
    expect(kill).not.toHaveTextContent("Apply to host to take effect.");
    const leases = screen.getByRole("region", { name: "DHCP leases" });
    expect(leases).toHaveTextContent("7 active");
    expect(within(leases).getAllByRole("listitem")).toHaveLength(7);
    expect(screen.queryByRole("region", { name: "Gateway Segment" })).toBeNull();

    for (const [name, summary] of [["Gateway Segment", null], ["LAN access & IPv6", "LAN on · IPv6 on · static /64"], ["Gateway DNS", "off"], ["Router checklist", "6 steps"]] as const) {
      expect(header(name)).toHaveAttribute("aria-expanded", "false");
      if (summary) await waitFor(() => expect(header(name)).toHaveTextContent(summary));
    }
    expect(screen.getByLabelText("Segment interface", { selector: "input" })).not.toBeVisible();
    expect(footerButton("Apply to host")).toBeNull();
    expect(footerButton("Discard")).toBeNull();
  });

  it("an opened section shows its fields; an edit brings up the sticky footer, and Apply from it sends the form", async () => {
    const { api$ } = await openPhone();
    const success = vi.spyOn(toast, "success");
    await userEvent.click(header("Gateway Segment"));
    expect(header("Gateway Segment")).toHaveAttribute("aria-expanded", "true");
    const lease = within(section("Gateway Segment")).getByLabelText("DHCP lease", { selector: "input" });
    expect(lease).toBeVisible();
    await fill(lease, "24h");
    expect(await screen.findByText("● unsaved changes")).toBeInTheDocument();
    await waitFor(() => expect(footerButton("Apply to host")).toBeEnabled());
    expect(footerButton("Discard")).toBeEnabled();
    await userEvent.click(footerButton("Apply to host")!);
    await waitFor(() => expect(api$.putNetwork).toHaveBeenCalledWith(expect.objectContaining({ dhcp_lease: "24h" })));
    await waitFor(() => expect(success).toHaveBeenCalledWith("saved · network + DHCP applied to host", { duration: 8000 }));
    await waitFor(() => expect(footerButton("Apply to host")).toBeNull());
  });

  it("a section's summary follows the form; Discard puts it back and hides the footer", async () => {
    await openPhone();
    await userEvent.click(header("LAN access & IPv6"));
    await userEvent.click(within(section("LAN access & IPv6")).getByRole("switch", { name: "IPv6 (tunnel)" }));
    expect(within(section("LAN access & IPv6")).getByRole("switch", { name: "IPv6 (tunnel)" })).not.toBeChecked();
    expect(section("LAN access & IPv6")).not.toHaveTextContent("Flipping it while the tunnel runs restarts xray.");
    await userEvent.click(header("LAN access & IPv6"));
    expect(header("LAN access & IPv6")).toHaveTextContent("LAN on · IPv6 off");
    await userEvent.click(footerButton("Discard")!);
    await waitFor(() => expect(footerButton("Discard")).toBeNull());
    expect(header("LAN access & IPv6")).toHaveTextContent("LAN on · IPv6 on · static /64");
  });

  it("a section with an error stays open, so the reason Apply is unavailable stays in view", async () => {
    await openPhone();
    await userEvent.click(header("Gateway Segment"));
    await fill(within(section("Gateway Segment")).getByLabelText("DHCP range end", { selector: "input" }), "192.168.50.80");
    expect(await screen.findByText("DHCP pool must sit inside 192.168.50.0/24 with start ≤ end")).toBeVisible();
    await waitFor(() => expect(footerButton("Apply to host")).toBeDisabled());
    expect(screen.getByText("✕ DHCP pool invalid")).toBeInTheDocument();
    await userEvent.click(header("Gateway Segment"));
    expect(header("Gateway Segment")).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("DHCP pool must sit inside 192.168.50.0/24 with start ≤ end")).toBeVisible();
  });

  it("the gateway DNS switch sits beside its section's header, not in it, and flips with the section closed", async () => {
    const { api$ } = await openPhone();
    const switchControl = within(section("Gateway DNS")).getByRole("switch", { name: "Gateway DNS" });
    await waitFor(() => expect(switchControl).toBeEnabled());
    expect(header("Gateway DNS")).not.toContainElement(switchControl);
    await userEvent.click(switchControl);
    await waitFor(() => expect(api$.putSettings).toHaveBeenCalledWith({ dns_intercept: true }));
    expect(header("Gateway DNS")).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(header("Gateway DNS"));
    expect(section("Gateway DNS")).toHaveTextContent("Resolve segment DNS in the gateway over DoH · the intercept does not see client DNS while it points at the gateway.");
  });

  it("the router checklist lists titles; a tap opens a step's detail", async () => {
    await openPhone();
    await userEvent.click(header("Router checklist"));
    const steps = within(section("Router checklist")).getByRole("list");
    expect(steps.tagName).toBe("OL");
    const step = within(steps).getByRole("button", { name: "Disable the router's DHCP on VLAN 2" });
    expect(within(steps).getByText(/two DHCP servers on one VLAN conflict/)).not.toBeVisible();
    await userEvent.click(step);
    expect(step).toHaveAttribute("aria-expanded", "true");
    expect(within(steps).getByText(/two DHCP servers on one VLAN conflict/)).toBeVisible();
  });
});
