import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { ApiError } from "../../api/client";
import { keys } from "../../api/keys";
import { settleConfirm } from "../../components/confirm";
import { TUNNEL_ROUTING, mockApi, mockTunnel } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";

afterEach(() => act(() => settleConfirm(false)));

async function openRouting() {
  const api$ = mockTunnel(mockApi());
  const view = renderApp("/tunnel/routing");
  const table = await screen.findByRole("table", { name: "Routing rules" });
  return { api$, table, ...view };
}

const banner = () => screen.queryByRole("region", { name: "Staged changes" });
const bodyRows = (table: HTMLElement) => within(table).getAllByRole("row").slice(1);

describe("Routing › rules table (R1)", () => {
  it("the rules between the fixed private-ranges row and the default catch-all, with the footnote", async () => {
    const { table } = await openRouting();
    const rows = bodyRows(table);
    expect(rows[0]).toHaveAttribute("data-anchor", "first");
    expect(rows[0]).toHaveTextContent("fixedipprivate rangesdirectimplicit, always first");
    expect(rows.at(-1)).toHaveAttribute("data-anchor", "last");
    expect(rows.at(-1)).toHaveTextContent("fixedalleverything elseproxydefault catch-all, always last");
    expect(within(table).getByLabelText("Rule 2 value")).toHaveValue("ru");
    expect(within(table).getByRole("combobox", { name: "Rule 2 type" })).toHaveValue("geoip");
    expect(within(table).getByRole("combobox", { name: "Rule 2 action" })).toHaveValue("direct");
    expect(within(table).getByRole("combobox", { name: "Rule 2 action" })).toHaveClass("text-g1");
    expect(within(table).getByRole("combobox", { name: "Rule 1 action" })).toHaveClass("text-bad");
    expect(within(table).getByRole("combobox", { name: "Rule 4 action" })).toHaveClass("text-g2");
    expect(within(table).getByRole("textbox", { name: "Rule 5 label" })).toHaveValue("no SMTP");
    expect(within(table).getByRole("switch", { name: "Rule 6 enabled" })).toHaveAttribute("aria-checked", "false");
    expect(screen.getByText("DNS / QUIC / stats rules are injected automatically when those features are enabled.")).toBeInTheDocument();
    expect(screen.getByText("6 rules · checked top to bottom, first match wins")).toBeInTheDocument();
    expect(screen.getByText("6 / 256 rules")).toBeInTheDocument();
    expect(banner()).toBeNull();
  });

  it("each rule's value placeholder follows its type, and geo rules offer the geo suggestions", async () => {
    const { table } = await openRouting();
    const geo = within(table).getByLabelText("Rule 2 value");
    const listId = geo.getAttribute("list");
    expect(listId).toBeTruthy();
    const options = [...document.getElementById(listId!)!.querySelectorAll("option")].map((option) => option.value);
    expect(options).toEqual(["ru", "cn", "private", "category-ru", "category-ads-all", "geolocation-!cn", "google", "telegram"]);
    expect(within(table).getByLabelText("Rule 1 value")).toHaveAttribute("list", listId);
    expect(within(table).getByLabelText("Rule 3 value")).not.toHaveAttribute("list");
    await userEvent.click(within(table).getByRole("button", { name: "Add rule" }));
    const added = within(table).getByLabelText("Rule 7 value");
    expect(added).toHaveAttribute("placeholder", "example.com, domain:ya.ru");
    await userEvent.selectOptions(within(table).getByRole("combobox", { name: "Rule 7 type" }), "port");
    expect(added).toHaveAttribute("placeholder", "443 | 1000-2000 | 80,443");
  });

  it("shows the backend's structural problems under a rule's value", async () => {
    const { table } = await openRouting();
    const port = within(table).getByLabelText("Rule 5 value");
    await userEvent.clear(port);
    await userEvent.type(port, "70000");
    expect(port).toHaveAccessibleDescription('bad port "70000" — use 443, 1000-2000 or 80,443 within 1–65535');
    expect(port).toHaveAttribute("aria-invalid", "true");
    const ip = within(table).getByLabelText("Rule 4 value");
    await userEvent.clear(ip);
    await userEvent.type(ip, "10.0.0.0/8");
    expect(ip).toHaveAccessibleDescription('"10.0.0.0/8" is a private range — the built-in private → direct rule is matched first, so only direct works');
    await userEvent.selectOptions(within(table).getByRole("combobox", { name: "Rule 4 action" }), "direct");
    expect(ip).not.toHaveAccessibleDescription();
  });

  it("a ruleset with no rules says where traffic goes", async () => {
    const api$ = mockApi();
    api$.getRouting.mockResolvedValue({ rules: [], default_action: "direct", domain_strategy: "AsIs" });
    renderApp("/tunnel/routing");
    const table = await screen.findByRole("table", { name: "Routing rules" });
    expect(within(table).getByText("no rules — all traffic follows the default action")).toBeInTheDocument();
    expect(bodyRows(table).at(-1)).toHaveTextContent("everything elsedirect");
  });

  it("loading shows skeleton rows, never the empty message; a failed load is an error with Retry", async () => {
    const api$ = mockApi();
    let finish: () => void = () => {};
    api$.getRouting.mockImplementation(() => new Promise((resolve) => { finish = () => resolve(TUNNEL_ROUTING); }));
    renderApp("/tunnel/routing");
    expect(await screen.findByRole("region", { name: "Rules" })).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByText("no rules — all traffic follows the default action")).toBeNull();
    await act(async () => finish());
    expect(await screen.findByRole("table", { name: "Routing rules" })).toBeInTheDocument();
  });

  it("an error with Retry that loads the rules", async () => {
    const api$ = mockApi();
    api$.getRouting.mockRejectedValue(new ApiError(500, "boom"));
    renderApp("/tunnel/routing");
    const error = await screen.findByRole("alert", {}, { timeout: 3000 });
    expect(error).toHaveTextContent("Routing did not load");
    api$.getRouting.mockResolvedValue(TUNNEL_ROUTING);
    await userEvent.click(within(error).getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("table", { name: "Routing rules" })).toBeInTheDocument();
  });
});

describe("Routing › staging (R2, R3, R5)", () => {
  it("an edit stages it: STAGED · 1 change, politely; typing it back unstages", async () => {
    const { table } = await openRouting();
    const before = screen.getAllByRole("status");
    const label = within(table).getByRole("textbox", { name: "Rule 3 label" });
    await userEvent.type(label, "yandex");
    const status = within(banner()!).getByRole("status");
    // The live region was there, empty, before anything was staged — so the first STAGED sentence is announced.
    expect(before).toContain(status);
    expect(status).toHaveTextContent("STAGED · 1 change — not yet applied to the live config.");
    await userEvent.clear(label);
    expect(banner()).toBeNull();
    expect(status).toBeInTheDocument();
    expect(status).toBeEmptyDOMElement();
  });

  it("add, move and remove: up is off on the first rule, down on the last; counts follow the rules", async () => {
    const { table } = await openRouting();
    expect(within(table).getByRole("button", { name: "Move rule 1 up" })).toBeDisabled();
    expect(within(table).getByRole("button", { name: "Move rule 6 down" })).toBeDisabled();
    await userEvent.click(within(table).getByRole("button", { name: "Move rule 1 down" }));
    expect(within(table).getByLabelText("Rule 1 value")).toHaveValue("ru");
    expect(within(table).getByLabelText("Rule 2 value")).toHaveValue("category-ads-all");
    expect(banner()).toHaveTextContent("STAGED · 2 changes");
    await userEvent.click(within(table).getByRole("button", { name: "Move rule 2 up" }));
    expect(banner()).toBeNull();

    await userEvent.click(within(table).getByRole("button", { name: "Add rule" }));
    const added = within(table).getByLabelText("Rule 7 value");
    expect(added).toHaveValue("");
    expect(within(table).getByRole("combobox", { name: "Rule 7 action" })).toHaveValue("proxy");
    expect(added).toHaveAccessibleDescription("value required");
    expect(banner()).toHaveTextContent("STAGED · 0 changes");
    await userEvent.type(added, "example.org");
    expect(banner()).toHaveTextContent("STAGED · 1 change");
    await userEvent.click(within(table).getByRole("button", { name: "Remove rule 3" }));
    expect(banner()).toHaveTextContent("STAGED · 2 changes");
    expect(screen.getByText("6 rules · checked top to bottom, first match wins")).toBeInTheDocument();
  });

  it("the default action and domain strategy are staged; the catch-all row follows the default", async () => {
    const { table } = await openRouting();
    const defaults = screen.getByRole("region", { name: "Defaults" });
    await userEvent.selectOptions(within(defaults).getByLabelText("Default action"), "block");
    expect(bodyRows(table).at(-1)).toHaveTextContent("everything elseblock");
    expect(within(defaults).getByLabelText("Default action")).toHaveClass("text-bad");
    await userEvent.selectOptions(within(defaults).getByLabelText("Domain strategy"), "IPOnDemand");
    expect(banner()).toHaveTextContent("STAGED · 2 changes");
    expect(within(defaults).getByText("Saving with default = block asks to confirm first.")).toBeInTheDocument();
  });

  it("Discard drops the staged edits without asking", async () => {
    const { table } = await openRouting();
    await userEvent.click(within(table).getByRole("button", { name: "Remove rule 1" }));
    await userEvent.click(within(banner()!).getByRole("button", { name: "Discard" }));
    expect(screen.queryByRole("dialog", { name: "Confirm" })).toBeNull();
    expect(banner()).toBeNull();
    expect(within(table).getByLabelText("Rule 1 value")).toHaveValue("category-ads-all");
  });

  it("follows the gateway while nothing is staged; while staged keeps the edits and says the gateway changed", async () => {
    const { api$, client, table } = await openRouting();
    const changed = { ...TUNNEL_ROUTING, rules: TUNNEL_ROUTING.rules.slice(1) };
    api$.getRouting.mockResolvedValue(changed);
    await act(() => client.invalidateQueries({ queryKey: keys.routing }));
    await waitFor(() => expect(within(table).getByLabelText("Rule 1 value")).toHaveValue("ru"));
    expect(banner()).toBeNull();

    await userEvent.type(within(table).getByRole("textbox", { name: "Rule 1 label" }), " — mine");
    const status = within(banner()!).getByRole("status");
    api$.getRouting.mockResolvedValue(TUNNEL_ROUTING);
    await act(() => client.invalidateQueries({ queryKey: keys.routing }));
    // Said in the staged live region, which was there before the gateway moved, so the notice is announced too.
    expect(await within(status).findByText("The ruleset changed on the gateway since you started editing — Discard to load it")).toBeInTheDocument();
    expect(within(banner()!).getByRole("status")).toBe(status);
    expect(within(table).getByRole("textbox", { name: "Rule 1 label" })).toHaveValue("RU off tunnel — mine");
    expect(within(table).queryByLabelText("Rule 6 value")).toBeNull();

    await userEvent.click(within(banner()!).getByRole("button", { name: "Discard" }));
    expect(within(table).getByLabelText("Rule 1 value")).toHaveValue("category-ads-all");
    expect(within(table).getByLabelText("Rule 6 value")).toHaveValue("netflix.com");
  });

  it("a staged session that is typed back to the start follows the gateway again", async () => {
    const { api$, client, table } = await openRouting();
    const label = within(table).getByRole("textbox", { name: "Rule 1 label" });
    await userEvent.type(label, "!");
    await userEvent.type(label, "{Backspace}");
    api$.getRouting.mockResolvedValue({ ...TUNNEL_ROUTING, default_action: "direct" });
    await act(() => client.invalidateQueries({ queryKey: keys.routing }));
    await waitFor(() => expect(bodyRows(table).at(-1)).toHaveTextContent("everything elsedirect"));
    expect(banner()).toBeNull();
  });
});
