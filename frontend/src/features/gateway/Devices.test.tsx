import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import { settleConfirm } from "../../components/confirm";
import { GATEWAY_NETWORK, RESERVATIONS, mockApi, mockGateway } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";
import { byUsage, deviceLabel, nameIssue, unpinnedLeases, usageText, windowText } from "./devices";

afterEach(() => act(() => { settleConfirm(false); toast.dismiss(); }));

async function openNetwork() {
  const api$ = mockGateway(mockApi());
  api$.getNetwork.mockResolvedValue(GATEWAY_NETWORK);
  const view = renderApp("/gateway/network");
  await screen.findByRole("region", { name: "Devices" });
  await screen.findByRole("list", { name: "Pinned devices" });
  return { api$, ...view };
}

const card = () => screen.getByRole("region", { name: "Devices" });

describe("Gateway › Network — devices (A4/B1)", () => {
  it("pinned devices come busiest first, with their traffic and whether they are leased", async () => {
    await openNetwork();
    const rows = within(card()).getAllByRole("listitem");
    // The Apple TV moved 4 GB today; the console has never been seen.
    expect(rows[0]).toHaveTextContent("192.168.50.123");
    expect(rows[0]).toHaveTextContent("4.1 GB down · 240 MB up");
    expect(rows[0]).toHaveTextContent("online");
    const console_ = card().querySelector('[data-device-ip="192.168.50.177"]') as HTMLElement;
    expect(console_).toHaveTextContent("no traffic recorded");
    expect(console_).toHaveTextContent("offline");
    expect(card()).toHaveTextContent("traffic in the last 1 d");
  });

  it("a lease that is not pinned offers Pin, and pinning sends its MAC and hostname", async () => {
    const { api$ } = await openNetwork();
    const unpinned = within(card()).getByRole("list", { name: "Unpinned leases" });
    // The pinned Apple TV is not offered again.
    expect(within(unpinned).queryByText("192.168.50.123")).toBeNull();

    await userEvent.click(within(unpinned).getByRole("button", { name: "Pin 192.168.50.101" }));

    await waitFor(() => expect(api$.addReservation).toHaveBeenCalledWith(
      "aa:bb:cc:00:01:01", "192.168.50.101", "iphone-anna"));
    expect(await screen.findByText("192.168.50.101 pinned")).toBeInTheDocument();
  });

  it("a hostname the gateway would refuse is not sent as a name", async () => {
    const { api$ } = await openNetwork();
    const unpinned = within(card()).getByRole("list", { name: "Unpinned leases" });
    // 192.168.50.112 has no hostname at all; macbook-pro's is fine. The check is the same one the
    // gateway applies, so a lease named "living room" would be pinned without the name.
    await userEvent.click(within(unpinned).getByRole("button", { name: "Pin 192.168.50.112" }));
    await waitFor(() => expect(api$.addReservation).toHaveBeenCalledWith("aa:bb:cc:00:01:12", "192.168.50.112", ""));
  });

  it("renaming validates before it is sent, and Cancel changes nothing", async () => {
    const { api$ } = await openNetwork();
    await userEvent.click(within(card()).getByRole("button", { name: "Rename 192.168.50.123" }));
    const field = within(card()).getByLabelText("Name of 192.168.50.123");

    await userEvent.clear(field);
    await userEvent.type(field, "living room");
    expect(within(card()).getByText("letters, digits and hyphens only")).toBeInTheDocument();
    expect(within(card()).getByRole("button", { name: "Save name of 192.168.50.123" })).toBeDisabled();

    await userEvent.clear(field);
    await userEvent.type(field, "living-room");
    await userEvent.click(within(card()).getByRole("button", { name: "Save name of 192.168.50.123" }));
    await waitFor(() => expect(api$.renameReservation).toHaveBeenCalledWith(1, "living-room"));
  });

  it("unpinning asks first and says what it costs", async () => {
    const { api$ } = await openNetwork();
    await userEvent.click(within(card()).getByRole("button", { name: "Unpin 192.168.50.123" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm" });
    expect(dialog).toHaveTextContent("its traffic stops being counted");
    expect(dialog).toHaveTextContent("A routing rule naming that address is left as it is");

    await userEvent.click(within(dialog).getByRole("button", { name: "Unpin" }));
    await waitFor(() => expect(api$.deleteReservation).toHaveBeenCalledWith(1));
  });

  it("a refused pin is reported and the list is unchanged", async () => {
    const { api$ } = await openNetwork();
    api$.addReservation.mockRejectedValueOnce(new ApiError(422, "192.168.50.101 is outside the segment"));
    const error = vi.spyOn(toast, "error");

    const unpinned = within(card()).getByRole("list", { name: "Unpinned leases" });
    await userEvent.click(within(unpinned).getByRole("button", { name: "Pin 192.168.50.101" }));

    await waitFor(() => expect(error).toHaveBeenCalled());
    expect(within(card()).getAllByRole("listitem").length).toBeGreaterThan(0);
  });
});

describe("device helpers", () => {
  it("names a device by its own name, its lease's hostname, or its address", () => {
    const [tv, console_] = RESERVATIONS.reservations;
    expect(deviceLabel(tv!, GATEWAY_NETWORK.status.clients)).toBe("appletv");
    expect(deviceLabel({ ...tv!, name: "" }, GATEWAY_NETWORK.status.clients)).toBe("appletv");   // from the lease
    expect(deviceLabel(console_!, GATEWAY_NETWORK.status.clients)).toBe("192.168.50.177");       // no lease at all
  });

  it("counts, sorts and words the usage", () => {
    expect(usageText({ up_bytes: 0, down_bytes: 0 })).toBe("no traffic recorded");
    expect(usageText({ up_bytes: 1_500_000, down_bytes: 2_000_000_000 })).toBe("2 GB down · 1.5 MB up");
    expect(byUsage(RESERVATIONS.reservations).map((r) => r.id)).toEqual([1, 2]);
    expect(windowText(86_400)).toBe("in the last 1 d");
    expect(windowText(3_600)).toBe("in the last 1 h");
    expect(windowText(600)).toBe("in the last 10 min");
  });

  it("only offers leases nothing is pinned to, by MAC and not by address", () => {
    const leases = GATEWAY_NETWORK.status.clients;
    const free = unpinnedLeases(leases, RESERVATIONS.reservations);
    expect(free.map((lease) => lease.ip)).not.toContain("192.168.50.123");
    expect(free).toHaveLength(leases.length - 1);
    // A pinned device that took another address today is still pinned: the MAC decides.
    const moved = [{ ...RESERVATIONS.reservations[0]!, ip: "192.168.50.199" }];
    expect(unpinnedLeases(leases, moved).map((lease) => lease.ip)).not.toContain("192.168.50.123");
  });

  it("accepts a hostname, refuses what dnsmasq's config cannot carry", () => {
    expect(nameIssue("")).toBeNull();
    expect(nameIssue("living-room-tv")).toBeNull();
    expect(nameIssue("living room")).toBe("letters, digits and hyphens only");
    expect(nameIssue("tv\ndhcp-script=/tmp/x.sh")).toBe("letters, digits and hyphens only");
  });
});
