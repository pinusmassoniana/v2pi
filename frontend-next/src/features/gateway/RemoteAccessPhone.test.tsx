import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Status } from "../../api/client";
import { keys } from "../../api/keys";
import { settleConfirm } from "../../components/confirm";
import { RW_CLIENTS, RW_PENDING, STATUS, mockApi } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";
import { setViewportWidth } from "../../test/viewport";

afterEach(() => act(() => settleConfirm(false)));

async function openPhone(status: Status = STATUS) {
  setViewportWidth(390);
  const api$ = mockApi();
  api$.getStatus.mockResolvedValue(status);
  const view = renderApp("/gateway/remote-access");
  await screen.findByRole("list", { name: "Clients" });
  await waitFor(() => expect(view.client.getQueryData(keys.status)).toBeDefined());
  return { api$, ...view };
}

const card = (name: string) => within(screen.getByRole("list", { name: "Clients" })).getByRole("listitem", { name });
const header = (name: string) => screen.getByRole("button", { name: new RegExp(`^${name}`) });
const section = (name: string) => screen.getByRole("group", { name: new RegExp(`^${name}`) });

describe("Remote access on a phone", () => {
  it("the enable switch, the clients as cards, the inbound fields, then collapsed hosts, subnets and checklist; no footer while clean", async () => {
    await openPhone();
    const access = screen.getByRole("region", { name: "Remote access" });
    expect(within(access).getByRole("switch", { name: "Accept inbound connections" })).toBeChecked();
    expect(access).toHaveTextContent("live");
    expect(screen.getByRole("region", { name: "Clients" })).toHaveTextContent("3 / 16");
    const anna = card("iphone-anna");
    expect(anna).toHaveTextContent("active");
    expect(within(anna).getByText("uuid hidden")).toBeInTheDocument();
    for (const name of ["Download .conf for iphone-anna", "Copy link for iphone-anna", "More actions for iphone-anna"]) expect(within(anna).getByRole("button", { name })).toBeEnabled();
    expect(card("ipad")).toHaveTextContent("suspended");
    expect(screen.queryByRole("table")).toBeNull();

    expect(within(section("Remote Access Inbound")).getByLabelText("Listen port", { selector: "input" })).toBeVisible();
    for (const [name, summary] of [["LAN hosts by name", "2 / 32"], ["Routed subnets", "192.168.1.0/24, 192.168.50.0/24"], ["Router checklist", "3 steps"]] as const) {
      expect(header(name)).toHaveAttribute("aria-expanded", "false");
      expect(header(name)).toHaveTextContent(summary);
    }
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("the ⋯ menu suspends at once; its Remove asks once the menu has closed", async () => {
    const { api$ } = await openPhone();
    const success = vi.spyOn(toast, "success");
    await userEvent.click(within(card("iphone-anna")).getByRole("button", { name: "More actions for iphone-anna" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Suspend" }));
    await waitFor(() => expect(api$.setRwClientEnabled).toHaveBeenCalledWith(RW_CLIENTS[0]!.id, false));
    await waitFor(() => expect(success).toHaveBeenCalledWith("client suspended — its uuid is kept — live tunnel rebuilt without it", { duration: 8000 }));

    const trigger = within(card("laptop-work")).getByRole("button", { name: "More actions for laptop-work" });
    await waitFor(() => expect(trigger).toBeEnabled());
    await userEvent.click(trigger);
    await userEvent.click(await screen.findByRole("menuitem", { name: "Remove…" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm" });
    expect(dialog).toHaveTextContent("Remove laptop-work? Its link and config stop working immediately.");
    await waitFor(() => expect(dialog).toHaveFocus());
    await userEvent.click(within(dialog).getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(api$.deleteRwClient).toHaveBeenCalledWith(RW_CLIENTS[2]!.id));
  });

  it("Resume from the menu asks before starting a stopped tunnel", async () => {
    const { api$ } = await openPhone({ ...STATUS, running: false, xray_state: "stopped" });
    await userEvent.click(within(card("ipad")).getByRole("button", { name: "More actions for ipad" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Resume" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm" });
    expect(dialog).toHaveTextContent("This starts the tunnel again. Continue?");
    await userEvent.click(within(dialog).getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(api$.setRwClientEnabled).toHaveBeenCalledWith(RW_CLIENTS[1]!.id, true));
  });

  it("Add client opens a sheet on its name field; adding closes it and gives focus back to Add client", async () => {
    const { api$ } = await openPhone();
    const add = screen.getByRole("button", { name: "Add client" });
    await userEvent.click(add);
    const sheet = await screen.findByRole("dialog", { name: "Add client" });
    const input = within(sheet).getByLabelText("Client name");
    await waitFor(() => expect(input).toHaveFocus());
    await userEvent.type(input, "e2e-phone{Enter}");
    await waitFor(() => expect(api$.addRwClient).toHaveBeenCalledWith("e2e-phone"));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Add client" })).toBeNull());
    await waitFor(() => expect(add).toHaveFocus());
    expect(await within(screen.getByRole("list", { name: "Clients" })).findByRole("listitem", { name: "e2e-phone" })).toBeInTheDocument();
  });

  it("an edit brings up the sticky Save; a hosts section holding an error stays open", async () => {
    const { api$ } = await openPhone();
    await userEvent.click(header("LAN hosts by name"));
    await userEvent.click(within(section("LAN hosts by name")).getByRole("button", { name: "Add host" }));
    await userEvent.type(within(section("LAN hosts by name")).getByLabelText("Host 3 name"), "tv.v2pi");
    expect(await within(section("LAN hosts by name")).findByText('host "tv.v2pi" needs both a name and an IP')).toBeVisible();
    await userEvent.click(header("LAN hosts by name"));
    expect(header("LAN hosts by name")).toHaveAttribute("aria-expanded", "true");
    const save = screen.getByRole("button", { name: "Save" });
    await userEvent.click(save);
    expect(api$.putRw).not.toHaveBeenCalled();
    await userEvent.type(within(section("LAN hosts by name")).getByLabelText("Host 3 IPv4"), "192.168.1.40");
    await waitFor(() => expect(within(section("LAN hosts by name")).queryByText(/needs both a name/)).toBeNull());
    await userEvent.click(save);
    await waitFor(() => expect(api$.putRw).toHaveBeenCalledWith(expect.objectContaining({ hosts: { "nas.v2pi": "192.168.1.10", "printer.v2pi": "192.168.1.20", "tv.v2pi": "192.168.1.40" } })));
  });

  it("the checklist opens a step's detail on a tap; a pending revocation shows above everything", async () => {
    const api$ = mockApi();
    api$.getRw.mockResolvedValue(RW_PENDING);
    setViewportWidth(390);
    renderApp("/gateway/remote-access");
    expect(await screen.findByRole("alert", { name: "SECURITY WARNING: xray's stop could not be confirmed, so the revoked device may still be able to connect." })).toBeInTheDocument();
    await userEvent.click(header("Router checklist"));
    const step = within(section("Router checklist")).getByRole("button", { name: /^DDNS in direct mode/ });
    await userEvent.click(step);
    expect(within(section("Router checklist")).getByText(/will not carry Reality's raw TCP/)).toBeVisible();
  });
});
