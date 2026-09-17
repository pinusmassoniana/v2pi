import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type RoutingIn } from "../../api/client";
import { settleConfirm } from "../../components/confirm";
import { ROUTING_INVALID, RU_DIRECT_PRESET, STATUS, holdConnectionWrite, mockApi, mockTunnel } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";
import { GATEWAY_CHANGED } from "./StagedBanner";

const writeText = vi.fn<(text: string) => Promise<void>>();

beforeEach(() => {
  writeText.mockReset().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  Object.defineProperty(document, "execCommand", { value: undefined, configurable: true, writable: true });
});
afterEach(() => act(() => settleConfirm(false)));

async function openRouting(options: { activeNode?: number | null } = {}) {
  const api$ = mockTunnel(mockApi());
  api$.getStatus.mockResolvedValue({ ...STATUS, active_node_id: options.activeNode === undefined ? 1 : options.activeNode });
  const view = renderApp("/tunnel/routing");
  const table = await screen.findByRole("table", { name: "Routing rules" });
  await waitFor(() => expect(api$.getStatus).toHaveBeenCalled());
  return { api$, table, ...view };
}

const banner = () => screen.queryByRole("region", { name: "Staged changes" });
const toolbarButton = (name: string) => screen.getAllByRole("button", { name }).find((button) => !banner()?.contains(button))!;

async function stageLabel(table: HTMLElement, text = "!") {
  await userEvent.type(within(table).getByRole("textbox", { name: "Rule 3 label" }), text);
}

async function answer(text: string, button: string) {
  const ask = await screen.findByRole("dialog", { name: "Confirm" });
  expect(ask).toHaveTextContent(text);
  await userEvent.click(within(ask).getByRole("button", { name: button }));
}

describe("Routing › Validate (R6)", () => {
  it("checks what Save would send and says so; an edit marks the result as out of date", async () => {
    const { api$, table } = await openRouting();
    const before = screen.getAllByRole("status");
    await userEvent.click(toolbarButton("Validate"));
    const result = await screen.findByText("✓ ruleset valid");
    expect(result).toHaveAttribute("role", "status");
    expect(before).toContain(result);   // an existing live region took the answer, so it is announced
    expect(result).toHaveClass("text-ok");
    expect(screen.getByText("· Validate never saves")).toBeInTheDocument();
    const sent = api$.validateRouting.mock.calls[0]![0];
    expect(sent.rules).toHaveLength(6);
    expect(sent.default_action).toBe("proxy");

    await stageLabel(table);
    expect(result).toHaveTextContent("Form changed since this run — run it again.");
    await userEvent.type(within(table).getByRole("textbox", { name: "Rule 3 label" }), "{Backspace}");
    expect(result).toHaveTextContent("✓ ruleset valid");
    expect(api$.getRouting).toHaveBeenCalledTimes(1);
  });

  it("a refusal and a failure both read as ✗", async () => {
    const { api$ } = await openRouting();
    api$.validateRouting.mockResolvedValueOnce(ROUTING_INVALID);
    await userEvent.click(toolbarButton("Validate"));
    expect(await screen.findByText(`✗ ${ROUTING_INVALID.error}`)).toHaveClass("text-bad");
    api$.validateRouting.mockRejectedValueOnce(new ApiError(0, "network error"));
    await userEvent.click(toolbarButton("Validate"));
    expect(await screen.findByText("✗ network error")).toBeInTheDocument();
  });
});

describe("Routing › Save (R6)", () => {
  it("is off until something is staged; sends trimmed, deduplicated rules and says the tunnel took them", async () => {
    const { api$, table } = await openRouting();
    const success = vi.spyOn(toast, "success");
    expect(toolbarButton("Save")).toBeDisabled();
    await userEvent.click(within(table).getByRole("button", { name: "Add rule" }));
    await userEvent.type(within(table).getByLabelText("Rule 7 value"), "  netflix.com ");
    const statusReads = api$.getStatus.mock.calls.length;
    await userEvent.click(toolbarButton("Save"));
    await waitFor(() => expect(api$.putRouting).toHaveBeenCalledTimes(1));
    const body: RoutingIn = api$.putRouting.mock.calls[0]![0];
    expect(body.rules.map((rule) => rule.value)).toEqual(["category-ads-all", "ru", "*.ya.ru, yandex.net", "45.83.0.0/16", "25", "netflix.com"]);
    expect(body.domain_strategy).toBe("IPIfNonMatch");
    await waitFor(() => expect(success).toHaveBeenCalledWith("saved & applied · 1 duplicate row(s) dropped", { duration: 8000 }));
    await waitFor(() => expect(banner()).toBeNull());
    expect(within(table).getByLabelText("Rule 1 value")).toHaveValue("category-ads-all");
    expect(within(table).queryByLabelText("Rule 7 value")).toBeNull();
    await waitFor(() => expect(api$.getStatus.mock.calls.length).toBeGreaterThan(statusReads));
  });

  it("with no active node the save applies on the next Connect; Apply staged in the banner saves too", async () => {
    const { api$, table } = await openRouting({ activeNode: null });
    const success = vi.spyOn(toast, "success");
    await stageLabel(table, "yandex");
    await userEvent.click(within(banner()!).getByRole("button", { name: "Apply staged" }));
    await waitFor(() => expect(success).toHaveBeenCalledWith("saved — applies on next Connect", { duration: 8000 }));
    expect(api$.putRouting.mock.calls[0]![0].rules[2]).toEqual({ type: "domain", value: "*.ya.ru, yandex.net", action: "direct", enabled: true, label: "yandex", dataset: "" });
  });

  it("a block default asks first; Cancel sends nothing", async () => {
    const { api$ } = await openRouting();
    await userEvent.selectOptions(within(screen.getByRole("region", { name: "Defaults" })).getByLabelText("Default action"), "block");
    await userEvent.click(toolbarButton("Save"));
    await answer("Default action is BLOCK — all non-matching traffic from the segment will be dropped. Continue?", "Cancel");
    expect(api$.putRouting).not.toHaveBeenCalled();
    await userEvent.click(toolbarButton("Save"));
    await answer("Default action is BLOCK", "Save");
    await waitFor(() => expect(api$.putRouting).toHaveBeenCalledWith(expect.objectContaining({ default_action: "block" })));
  });

  it("another connection change started while the block question was open: nothing is sent", async () => {
    const { api$, client } = await openRouting();
    const error = vi.spyOn(toast, "error");
    await userEvent.selectOptions(within(screen.getByRole("region", { name: "Defaults" })).getByLabelText("Default action"), "block");
    await userEvent.click(toolbarButton("Save"));
    await screen.findByRole("dialog", { name: "Confirm" });
    const release = holdConnectionWrite(client);
    await answer("Default action is BLOCK", "Save");
    expect(error).toHaveBeenCalledWith("Another connection change is still running — try again when it finishes", { duration: 20000 });
    expect(api$.putRouting).not.toHaveBeenCalled();
    expect(toolbarButton("Save")).toBeDisabled();
    await release();
    await waitFor(() => expect(toolbarButton("Save")).toBeEnabled());
  });

  it("a 502 is a save the tunnel could not apply: nothing was saved, and the staged edits stay for a retry", async () => {
    const { api$, table } = await openRouting();
    const error = vi.spyOn(toast, "error");
    api$.putRouting.mockRejectedValueOnce(new ApiError(502, "xray -test failed: bad config"));
    await stageLabel(table, "yandex");
    await userEvent.click(toolbarButton("Save"));
    await waitFor(() => expect(error).toHaveBeenCalledWith("not saved — applying to the tunnel failed: xray -test failed: bad config", { duration: 20000 }));
    // The store rolled the write back, so the gateway's ruleset never moved: no gateway-changed notice.
    await waitFor(() => expect(api$.getRouting).toHaveBeenCalledTimes(2));
    expect(banner()).toHaveTextContent("STAGED · 1 change");
    expect(banner()).not.toHaveTextContent(GATEWAY_CHANGED);
    expect(within(table).getByRole("textbox", { name: "Rule 3 label" })).toHaveValue("yandex");
    // Save again with the same edits — the rule was fixed, or just retried — and this time it goes through.
    await userEvent.click(toolbarButton("Save"));
    await waitFor(() => expect(api$.putRouting).toHaveBeenCalledTimes(2));
    expect(api$.putRouting.mock.calls[1]![0].rules[2]).toEqual({ type: "domain", value: "*.ya.ru, yandex.net", action: "direct", enabled: true, label: "yandex", dataset: "" });
    await waitFor(() => expect(banner()).toBeNull());
  });

  it("a 422 shows under the banner and keeps the staged edits", async () => {
    const { api$, table } = await openRouting();
    const error = vi.spyOn(toast, "error");
    api$.putRouting.mockRejectedValue(new ApiError(422, "rule 4: bad ip/cidr '45.83.0.0/33'"));
    await stageLabel(table);
    await userEvent.click(toolbarButton("Save"));
    expect(await screen.findByRole("alert")).toHaveTextContent("rule 4: bad ip/cidr '45.83.0.0/33'");
    expect(banner()).toHaveTextContent("STAGED · 1 change");
    expect(error).not.toHaveBeenCalled();
    await userEvent.click(within(banner()!).getByRole("button", { name: "Discard" }));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("stays off while another connection change runs", async () => {
    const { client, table } = await openRouting();
    await stageLabel(table);
    const release = holdConnectionWrite(client);
    await waitFor(() => expect(toolbarButton("Save")).toBeDisabled());
    expect(within(banner()!).getByRole("button", { name: "Apply staged" })).toBeDisabled();
    await release();
    await waitFor(() => expect(toolbarButton("Save")).toBeEnabled());
  });

  it("leaving with staged rules asks first", async () => {
    const { router, table } = await openRouting();
    await stageLabel(table);
    act(() => void router.navigate({ to: "/tunnel/anti-dpi" }));
    await answer("Discard unsaved changes and leave this screen?", "Cancel");
    expect(router.state.location.pathname).toBe("/tunnel/routing");
  });
});

describe("Routing › presets (R4)", () => {
  it("lists the presets; staging one replaces the rules with the reply, unsaved, without re-reading anything", async () => {
    const { api$, table } = await openRouting();
    const success = vi.spyOn(toast, "success");
    await userEvent.click(toolbarButton("Import preset"));
    const items = await screen.findAllByRole("menuitem");
    expect(items.map((item) => item.textContent)).toEqual([
      "ru-directRU-direct — keep Russian traffic off the tunnel", "block-adsBlock ads & trackers",
      "cn-directCN-direct — Chinese traffic off the tunnel", "lan-directLAN-direct — private ranges direct (explicit)",
      "ru-blocked-onlyOnly blocked-in-RU through the tunnel (needs the RU geo data)",
    ]);
    await userEvent.click(items[0]!);
    await waitFor(() => expect(api$.routingPreset).toHaveBeenCalledWith("ru-direct"));
    await waitFor(() => expect(within(table).getByLabelText("Rule 7 value")).toHaveValue("category-ru"));
    expect(success).toHaveBeenCalledWith('preset "ru-direct" staged — review and Save', { duration: 8000 });
    expect(banner()).toHaveTextContent("STAGED · 1 change");
    expect(api$.getRouting).toHaveBeenCalledTimes(1);
    expect(api$.putRouting).not.toHaveBeenCalled();
  });

  it("while a preset loads, the rules, defaults and tester are locked, so nothing typed meanwhile is lost to the reply", async () => {
    const { api$, table } = await openRouting();
    let land: () => void = () => {};
    api$.routingPreset.mockImplementationOnce(() => new Promise((resolve) => { land = () => resolve(RU_DIRECT_PRESET); }));
    await userEvent.click(toolbarButton("Import preset"));
    await userEvent.click(await screen.findByRole("menuitem", { name: /ru-direct/ }));
    await waitFor(() => expect(api$.routingPreset).toHaveBeenCalledWith("ru-direct"));
    await waitFor(() => expect(within(table).getByRole("textbox", { name: "Rule 3 label" })).toBeDisabled());
    expect(within(table).getByLabelText("Rule 1 value")).toBeDisabled();
    expect(within(table).getByRole("switch", { name: "Rule 1 enabled" })).toBeDisabled();
    expect(within(table).getByRole("button", { name: "Remove rule 1" })).toBeDisabled();
    expect(within(table).getByRole("button", { name: "Add rule" })).toBeDisabled();
    expect(within(screen.getByRole("region", { name: "Defaults" })).getByLabelText("Default action")).toBeDisabled();
    expect(within(screen.getByRole("region", { name: "Destination tester" })).getByRole("textbox")).toBeDisabled();
    await act(async () => land());
    await waitFor(() => expect(within(table).getByLabelText("Rule 7 value")).toHaveValue("category-ru"));
    expect(within(table).getByRole("textbox", { name: "Rule 3 label" })).toBeEnabled();
  });

  it("asks before replacing staged rules", async () => {
    const { api$, table } = await openRouting();
    await stageLabel(table);
    await userEvent.click(toolbarButton("Import preset"));
    await userEvent.click(await screen.findByRole("menuitem", { name: /block-ads/ }));
    await answer("Discard staged rules?", "Cancel");
    expect(api$.routingPreset).not.toHaveBeenCalled();
    expect(within(table).getByRole("textbox", { name: "Rule 3 label" })).toHaveValue("!");
  });
});

describe("Routing › JSON (R7) and Reset (R6)", () => {
  it("Export copies the ruleset as 2-space JSON", async () => {
    await openRouting();
    const success = vi.spyOn(toast, "success");
    await userEvent.click(toolbarButton("Export JSON"));
    await waitFor(() => expect(success).toHaveBeenCalledWith("ruleset copied as JSON", { duration: 8000 }));
    const text = writeText.mock.calls[0]![0];
    expect(text.startsWith('{\n  "rules": [\n    {\n      "type": "geosite",')).toBe(true);
    expect(JSON.parse(text)).toMatchObject({ default_action: "proxy", domain_strategy: "IPIfNonMatch" });
    writeText.mockRejectedValueOnce(new Error("denied"));
    const error = vi.spyOn(toast, "error");
    await userEvent.click(toolbarButton("Export JSON"));
    await waitFor(() => expect(error).toHaveBeenCalledWith("copy failed", { duration: 20000 }));
  });

  it("Export copies over plain HTTP, where there is no Clipboard API at all", async () => {
    await openRouting();
    const success = vi.spyOn(toast, "success");
    const copied: string[] = [];
    Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
    Object.defineProperty(document, "execCommand", {
      value: (command: string) => {
        copied.push(`${command}:${document.querySelector<HTMLTextAreaElement>('textarea[aria-hidden="true"]')?.value}`);
        return true;
      },
      configurable: true,
      writable: true,
    });
    await userEvent.click(toolbarButton("Export JSON"));
    await waitFor(() => expect(success).toHaveBeenCalledWith("ruleset copied as JSON", { duration: 8000 }));
    expect(copied).toHaveLength(1);
    expect(JSON.parse(copied[0]!.slice("copy:".length))).toMatchObject({ default_action: "proxy", domain_strategy: "IPIfNonMatch" });
  });

  it("Import explains a bad paste, then replaces the rules with a good one", async () => {
    const { table } = await openRouting();
    const success = vi.spyOn(toast, "success");
    await userEvent.click(toolbarButton("Import JSON"));
    const sheet = await screen.findByRole("dialog", { name: "Import JSON" });
    const box = within(sheet).getByLabelText("Paste a ruleset");
    fireEvent.change(box, { target: { value: "{rules" } });
    await userEvent.click(within(sheet).getByRole("button", { name: "Import" }));
    expect(within(sheet).getByRole("alert")).toHaveTextContent("invalid JSON");
    fireEvent.change(box, { target: { value: '{"default_action":"direct"}' } });
    await userEvent.click(within(sheet).getByRole("button", { name: "Import" }));
    expect(within(sheet).getByRole("alert")).toHaveTextContent("no rules array / bad rule shape");
    fireEvent.change(box, { target: { value: '{"rules":[{"type":"port","value":"25","action":"block","label":"no SMTP"}],"default_action":"direct"}' } });
    await userEvent.click(within(sheet).getByRole("button", { name: "Import" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Import JSON" })).toBeNull());
    expect(success).toHaveBeenCalledWith("imported — review and Save", { duration: 8000 });
    expect(within(table).getByLabelText("Rule 1 value")).toHaveValue("25");
    expect(within(table).queryByLabelText("Rule 2 value")).toBeNull();
    expect(within(screen.getByRole("region", { name: "Defaults" })).getByLabelText("Default action")).toHaveValue("direct");
  });

  it("Import over staged rules asks first", async () => {
    const { table } = await openRouting();
    await stageLabel(table);
    await userEvent.click(toolbarButton("Import JSON"));
    const sheet = await screen.findByRole("dialog", { name: "Import JSON" });
    fireEvent.change(within(sheet).getByLabelText("Paste a ruleset"), { target: { value: "[]" } });
    await userEvent.click(within(sheet).getByRole("button", { name: "Import" }));
    await answer("Discard staged rules?", "Cancel");
    expect(screen.getByRole("dialog", { name: "Import JSON" })).toBeInTheDocument();
    expect(within(table).getByRole("textbox", { name: "Rule 3 label", hidden: true })).toHaveValue("!");
  });

  it("Reset asks, then stages no rules and a proxy default without calling the gateway", async () => {
    const { api$, table } = await openRouting();
    await userEvent.selectOptions(within(screen.getByRole("region", { name: "Defaults" })).getByLabelText("Default action"), "block");
    await userEvent.click(toolbarButton("Reset"));
    await answer("Reset to the default ruleset (no rules, default = proxy)?", "Reset");
    expect(within(table).getByText("no rules — all traffic follows the default action")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Defaults" })).getByLabelText("Default action")).toHaveValue("proxy");
    expect(banner()).toHaveTextContent("STAGED · 6 changes");
    expect(api$.putRouting).not.toHaveBeenCalled();
  });
});

describe("Routing › destination tester (R8)", () => {
  it("answers over the staged rules as they are typed", async () => {
    const { table } = await openRouting();
    const tester = screen.getByRole("region", { name: "Destination tester" });
    expect(tester).toHaveTextContent("how would this host/port be routed? (literal rules only — geo not evaluated locally)");
    const input = within(tester).getByLabelText("how would this host/port be routed? (literal rules only — geo not evaluated locally)");
    expect(input).toHaveAttribute("placeholder", "example.com  |  1.2.3.4  |  1.2.3.4:443");
    await userEvent.type(input, "mail.example.com:25");
    expect(within(tester).getByRole("status")).toHaveTextContent('→ block (matched port "25" · no SMTP)');
    await userEvent.click(within(table).getByRole("switch", { name: "Rule 5 enabled" }));
    expect(within(tester).getByRole("status")).toHaveTextContent("→ proxy (default · geo rules not evaluated locally)");
    await userEvent.clear(input);
    await userEvent.type(input, "192.168.1.20");
    expect(within(tester).getByRole("status")).toHaveTextContent("→ direct (private range, always matched first)");
    await userEvent.clear(input);
    await userEvent.type(input, "2a00:1450:4010::65");
    expect(within(tester).getByRole("status")).toHaveTextContent("IPv6 preview is not evaluated locally — Validate/Save uses Xray's matcher.");
  });

  it("A5: the live answer comes from the running tunnel, and both are shown when they differ", async () => {
    const { api$ } = await openRouting();
    const tester = screen.getByRole("region", { name: "Destination tester" });
    const input = within(tester).getByRole("textbox");
    api$.testRoute.mockResolvedValueOnce({
      ok: true, outbound: "block", host: "doubleclick.net", port: 443, network: "tcp", source_ip: "", error: "",
    });

    await userEvent.type(input, "doubleclick.net");
    // The staged answer says proxy (no literal rule matches); the live one evaluates geosite.
    await userEvent.click(within(tester).getByRole("button", { name: "Test live" }));

    await waitFor(() => expect(api$.testRoute).toHaveBeenCalledWith("doubleclick.net", "tcp", ""));
    expect(await within(tester).findByText(/live · doubleclick.net:443 tcp/)).toBeInTheDocument();
    expect(tester).toHaveTextContent("the live answer differs from the staged one — these edits are not applied yet");
  });

  it("A5: a live answer the gateway cannot give is shown as the reason, not as a route", async () => {
    const { api$ } = await openRouting();
    const tester = screen.getByRole("region", { name: "Destination tester" });
    api$.testRoute.mockResolvedValueOnce({
      ok: false, outbound: "", host: "x.example", port: 443, network: "tcp", source_ip: "",
      error: "xray is not running — start the tunnel to ask it",
    });

    await userEvent.type(within(tester).getByRole("textbox"), "x.example");
    await userEvent.click(within(tester).getByRole("button", { name: "Test live" }));

    expect(await within(tester).findByText("xray is not running — start the tunnel to ask it")).toBeInTheDocument();
  });

  it("A5: the answer can be turned into a rule, staged at the top", async () => {
    const { table, api$ } = await openRouting();
    const tester = screen.getByRole("region", { name: "Destination tester" });
    api$.testRoute.mockResolvedValueOnce({
      ok: true, outbound: "proxy", host: "news.example", port: 443, network: "tcp", source_ip: "", error: "",
    });

    await userEvent.type(within(tester).getByRole("textbox"), "news.example");
    await userEvent.click(within(tester).getByRole("button", { name: "Test live" }));
    await within(tester).findByText(/live · news.example/);
    await userEvent.click(within(tester).getByRole("button", { name: "direct" }));

    // Staged at position 1, so it decides before the rules that already answered.
    expect(within(table).getByLabelText("Rule 1 value")).toHaveValue("news.example");
    expect(within(table).getByLabelText("Rule 1 action")).toHaveValue("direct");
    expect(banner()).toHaveTextContent("STAGED");
    expect(api$.putRouting).not.toHaveBeenCalled();
  });

  it("A5: a pinned device can be asked as, once the gateway has one", async () => {
    const { api$ } = await openRouting();
    const tester = screen.getByRole("region", { name: "Destination tester" });
    await userEvent.type(within(tester).getByRole("textbox"), "example.com");

    await userEvent.selectOptions(within(tester).getByLabelText("As device"), "192.168.50.123");
    await userEvent.selectOptions(within(tester).getByLabelText("Network"), "udp");
    await userEvent.click(within(tester).getByRole("button", { name: "Test live" }));

    await waitFor(() => expect(api$.testRoute).toHaveBeenCalledWith("example.com", "udp", "192.168.50.123"));
  });
});
