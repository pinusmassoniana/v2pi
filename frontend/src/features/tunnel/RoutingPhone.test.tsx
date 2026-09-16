import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { settleConfirm } from "../../components/confirm";
import { RU_DIRECT_PRESET, STATUS, mockApi, mockTunnel } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";
import { setViewportWidth } from "../../test/viewport";
import type { RuleRow } from "./rules";
import { RuleSheet } from "./RuleSheet";

afterEach(() => act(() => settleConfirm(false)));

async function openPhone() {
  setViewportWidth(390);
  const api$ = mockTunnel(mockApi());
  api$.getStatus.mockResolvedValue({ ...STATUS, active_node_id: null });
  const view = renderApp("/tunnel/routing");
  const list = await screen.findByRole("list", { name: "Routing rules" });
  return { api$, list, ...view };
}

const cards = (list: HTMLElement) => within(list).getAllByRole("listitem");
const banner = () => screen.queryByRole("region", { name: "Staged changes" });

describe("Routing on a phone", () => {
  it("rules are cards between the fixed ones; no table, no toolbar; Validate and Save in a sticky footer", async () => {
    const { list } = await openPhone();
    const items = cards(list);
    expect(items).toHaveLength(8);
    expect(items[0]).toHaveTextContent("fixedipprivate rangesdirectimplicit, always first");
    expect(items[2]).toHaveTextContent("2geoiprudirectRU off tunnel");
    expect(within(items[2]!).getByText("direct")).toHaveAttribute("data-action", "direct");
    expect(items[3]).toHaveTextContent("no label");
    expect(items[7]).toHaveTextContent("fixedalleverything elseproxydefault catch-all, always last");
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByRole("button", { name: "Import preset" })).toBeNull();
    const footer = screen.getByRole("button", { name: "Save" }).parentElement!;
    expect(footer).toHaveClass("sticky");
    expect(within(footer).getByRole("button", { name: "Save" })).toBeDisabled();
    expect(within(footer).getByText("Validate never saves")).toBeInTheDocument();
  });

  it("a card's switch stages the rule on or off without opening it; the banner sticks under the tabs", async () => {
    const { list } = await openPhone();
    await userEvent.click(within(cards(list)[6]!).getByRole("switch", { name: "Rule 6 enabled" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(banner()).toHaveTextContent("STAGED · 1 change");
    expect(banner()).toHaveClass("max-md:sticky");
  });

  it("a rule's sheet edits it in place: type, value with geo chips, action, label, on/off", async () => {
    const { list } = await openPhone();
    const open = within(list).getByRole("button", { name: "Edit rule 2" });
    await userEvent.click(open);
    const sheet = await screen.findByRole("dialog", { name: "Rule 2 of 6" });
    expect(within(sheet).getByRole("radio", { name: "geoip" })).toBeChecked();
    const chips = within(sheet).getByRole("group", { name: "Geo suggestions" });
    expect(within(chips).getByRole("button", { name: "ru" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(chips).getByRole("button", { name: "cn" }));
    expect(within(sheet).getByLabelText("Value")).toHaveValue("ru, cn");
    await userEvent.click(within(chips).getByRole("button", { name: "ru" }));
    expect(within(sheet).getByLabelText("Value")).toHaveValue("cn");
    const action = within(sheet).getByRole("group", { name: "Action" });
    await userEvent.click(within(action).getByRole("radio", { name: "block" }));
    expect(within(action).getByRole("radio", { name: "block" }).closest("label")).toHaveClass("has-[:checked]:text-bad");
    await userEvent.clear(within(sheet).getByLabelText("Label"));
    await userEvent.type(within(sheet).getByLabelText("Label"), "CN");
    await userEvent.click(within(sheet).getByRole("switch", { name: "Enabled" }));
    await userEvent.click(within(sheet).getByRole("radio", { name: "port" }));
    expect(within(sheet).queryByRole("group", { name: "Geo suggestions" })).toBeNull();
    expect(within(sheet).getByLabelText("Value")).toHaveAccessibleDescription('bad port "cn" — use 443, 1000-2000 or 80,443 within 1–65535');
    await userEvent.click(within(sheet).getByRole("button", { name: "Done" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(open).toHaveFocus();
    expect(cards(list)[2]).toHaveTextContent("portcnblockCN");
    expect(within(cards(list)[2]!).getByRole("switch", { name: "Rule 2 enabled" })).toHaveAttribute("aria-checked", "false");
    expect(banner()).toHaveTextContent("STAGED · 1 change");
  });

  it("a rule's sheet opens with focus on the sheet itself, not on a Type option the rule does not have", async () => {
    const { list } = await openPhone();
    await userEvent.click(within(list).getByRole("button", { name: "Edit rule 3" }));
    const sheet = await screen.findByRole("dialog", { name: "Rule 3 of 6" });
    expect(within(sheet).getByRole("radio", { name: "domain" })).toBeChecked();
    await waitFor(() => expect(sheet).toHaveFocus());
    expect(within(sheet).getByRole("radio", { name: "geoip" })).not.toHaveFocus();
  });

  it("moves a rule from its sheet, deletes it, and adds a new one that opens at once", async () => {
    const { list } = await openPhone();
    await userEvent.click(within(list).getByRole("button", { name: "Edit rule 1" }));
    let sheet = await screen.findByRole("dialog", { name: "Rule 1 of 6" });
    expect(within(sheet).getByRole("button", { name: "Move up" })).toBeDisabled();
    await userEvent.click(within(sheet).getByRole("button", { name: "Move down" }));
    sheet = await screen.findByRole("dialog", { name: "Rule 2 of 6" });
    await userEvent.click(within(sheet).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(cards(list)).toHaveLength(7);
    expect(banner()).toHaveTextContent("STAGED · 1 change");

    await userEvent.click(screen.getByRole("button", { name: "Add rule" }));
    sheet = await screen.findByRole("dialog", { name: "Rule 6 of 6" });
    expect(within(sheet).getByRole("radio", { name: "domain" })).toBeChecked();
    expect(within(sheet).getByRole("radio", { name: "proxy" })).toBeChecked();
    expect(within(sheet).getByText("required")).toBeInTheDocument();
    expect(within(sheet).getByRole("button", { name: "Move down" })).toBeDisabled();
    await userEvent.type(within(sheet).getByLabelText("Value"), "example.org");
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(banner()).toHaveTextContent("STAGED · 2 changes");
  });

  it("the ⋯ menu holds presets, JSON export and import, and Reset; the footer saves", async () => {
    const { api$, list } = await openPhone();
    const success = vi.spyOn(toast, "success");
    await userEvent.click(screen.getByRole("button", { name: "More routing actions" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).getAllByRole("menuitem").map((item) => item.textContent)).toEqual([
      "ru-directRU-direct — keep Russian traffic off the tunnel", "block-adsBlock ads & trackers",
      "cn-directCN-direct — Chinese traffic off the tunnel", "lan-directLAN-direct — private ranges direct (explicit)",
      "Export JSON", "Import JSON", "Reset",
    ]);
    await userEvent.click(within(menu).getByRole("menuitem", { name: "Reset" }));
    const ask = await screen.findByRole("dialog", { name: "Confirm" });
    await userEvent.click(within(ask).getByRole("button", { name: "Reset" }));
    await waitFor(() => expect(within(list).getByText("no rules — all traffic follows the default action")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api$.putRouting).toHaveBeenCalledWith({ rules: [], default_action: "proxy", domain_strategy: "IPIfNonMatch" }));
    await waitFor(() => expect(success).toHaveBeenCalledWith("saved — applies on next Connect", { duration: 8000 }));

    await userEvent.click(screen.getByRole("button", { name: "More routing actions" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Import JSON" }));
    expect(await screen.findByRole("dialog", { name: "Import JSON" })).toBeInTheDocument();
  });

  it("while a preset loads, the cards are locked, so nothing changed meanwhile is lost to the reply", async () => {
    const { api$, list } = await openPhone();
    let land: () => void = () => {};
    api$.routingPreset.mockImplementationOnce(() => new Promise((resolve) => { land = () => resolve(RU_DIRECT_PRESET); }));
    await userEvent.click(screen.getByRole("button", { name: "More routing actions" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: /ru-direct/ }));
    await waitFor(() => expect(api$.routingPreset).toHaveBeenCalledWith("ru-direct"));
    await waitFor(() => expect(within(list).getByRole("button", { name: "Edit rule 1" })).toBeDisabled());
    expect(within(list).getByRole("switch", { name: "Rule 1 enabled" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Add rule" })).toBeDisabled();
    await act(async () => land());
    await waitFor(() => expect(within(list).getByRole("button", { name: "Edit rule 1" })).toBeEnabled());
    expect(cards(list)).toHaveLength(9);
  });

  it("a locked rule sheet keeps its fields and rule actions off, and Done still closes it", async () => {
    const onClose = vi.fn();
    const row: RuleRow = { key: "r1", id: 1, type: "domain", value: "example.org", action: "proxy", enabled: true, label: "" };
    render(<RuleSheet row={row} index={0} count={2} disabled onUpdate={vi.fn()} onMove={vi.fn()} onRemove={vi.fn()} onClose={onClose} />);
    const sheet = await screen.findByRole("dialog", { name: "Rule 1 of 2" });
    expect(within(sheet).getByRole("radio", { name: "domain" })).toBeDisabled();
    expect(within(sheet).getByLabelText("Value")).toBeDisabled();
    expect(within(sheet).getByLabelText("Label")).toBeDisabled();
    expect(within(sheet).getByRole("switch", { name: "Enabled" })).toBeDisabled();
    for (const name of ["Move down", "Delete"]) expect(within(sheet).getByRole("button", { name })).toBeDisabled();
    await userEvent.click(within(sheet).getByRole("button", { name: "Done" }));
    expect(onClose).toHaveBeenCalled();
  });

  it("Validate from the footer shows its result there", async () => {
    await openPhone();
    const before = screen.getAllByRole("status");
    expect(screen.getByText("Validate never saves")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Validate" }));
    const result = await screen.findByText("✓ ruleset valid");
    expect(result.closest(".sticky")).not.toBeNull();
    expect(before).toContain(result);   // the footer's live region was mounted before the answer came
    expect(screen.queryByText("Validate never saves")).toBeNull();
  });
});
