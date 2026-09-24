import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type PreviewNodes } from "../../api/client";
import { settleConfirm } from "../../components/confirm";
import { PREVIEW_NODES, SUBS, mockApi } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";

afterEach(() => act(() => settleConfirm(false)));

async function openForm(which: "add" | string) {
  const api$ = mockApi();
  const view = renderApp("/nodes/subscriptions");
  const card = await screen.findByRole("region", { name: "work" });
  if (which === "add") await userEvent.click(screen.getByRole("button", { name: "Add subscription" }));
  else await userEvent.click(within(which === "work" ? card : screen.getByRole("region", { name: which })).getByRole("button", { name: "Edit" }));
  const sheet = await screen.findByRole("dialog", { name: which === "add" ? "Add subscription" : `Edit · ${which}` });
  return { api$, sheet, ...view };
}

describe("Add subscription (U7)", () => {
  it("starts with the default headers and no query params, and adds what was typed", async () => {
    const { api$, sheet } = await openForm("add");
    const success = vi.spyOn(toast, "success");
    expect(within(sheet).getByRole("textbox", { name: "Header 1 name" })).toHaveValue("x-device-os");
    expect(within(sheet).getByRole("textbox", { name: "Header 1 value" })).toHaveValue("{device_os}");
    expect(within(sheet).getByRole("textbox", { name: "Header 2 name" })).toHaveValue("user-agent");
    expect(within(sheet).getByRole("textbox", { name: "Header 2 value" })).toHaveValue("v2pi/1.0");
    expect(within(sheet).getByText("No query params")).toBeInTheDocument();
    expect(within(sheet).queryByRole("switch", { name: "Enabled" })).toBeNull();
    expect(within(sheet).queryByLabelText("Default tuning profile for new nodes")).toBeNull();

    await userEvent.type(within(sheet).getByLabelText("Name"), "big-feed");
    await userEvent.type(within(sheet).getByLabelText("URL"), "https://feed.example.net/api/sub?token=a41c09");
    await userEvent.clear(within(sheet).getByLabelText("Auto-update, min"));
    await userEvent.type(within(sheet).getByLabelText("Auto-update, min"), "60");
    await userEvent.click(within(sheet).getByRole("button", { name: "Add param" }));
    await userEvent.type(within(sheet).getByRole("textbox", { name: "Query param 1 name" }), "type");
    await userEvent.type(within(sheet).getByRole("textbox", { name: "Query param 1 value" }), "vless");
    await userEvent.click(within(sheet).getByRole("button", { name: "Remove header 2" }));
    await userEvent.click(within(sheet).getByRole("button", { name: "Add subscription" }));
    await waitFor(() => expect(api$.addSub).toHaveBeenCalledWith({
      name: "big-feed", url: "https://feed.example.net/api/sub?token=a41c09", interval_sec: 3_600,
      injection: { headers: { "x-device-os": "{device_os}" }, query: { type: "vless" } },
    }));
    await waitFor(() => expect(success).toHaveBeenCalledWith("Added big-feed", { duration: 8000 }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Add subscription" })).toBeNull());
  });

  it("name and URL are required and minutes whole; nothing is sent until they are", async () => {
    const { api$, sheet } = await openForm("add");
    await userEvent.clear(within(sheet).getByLabelText("Auto-update, min"));
    await userEvent.click(within(sheet).getByRole("button", { name: "Add subscription" }));
    expect(await within(sheet).findByText("name is required")).toBeInTheDocument();
    expect(within(sheet).getByText("URL is required")).toBeInTheDocument();
    expect(within(sheet).getByText("minutes must be a whole number, 0 or more")).toBeInTheDocument();
    expect(api$.addSub).not.toHaveBeenCalled();
  });

  it("a URL the gateway refuses is explained in the form, which keeps what was typed", async () => {
    const { api$, sheet } = await openForm("add");
    api$.addSub.mockRejectedValue(new ApiError(422, "url: subscription URL resolves to a non-public (internal) address"));
    await userEvent.type(within(sheet).getByLabelText("Name"), "lan");
    await userEvent.type(within(sheet).getByLabelText("URL"), "http://192.168.1.1/sub");
    await userEvent.click(within(sheet).getByRole("button", { name: "Add subscription" }));
    expect(await within(sheet).findByRole("alert")).toHaveTextContent("url: subscription URL resolves to a non-public (internal) address");
    expect(within(sheet).getByLabelText("URL")).toHaveValue("http://192.168.1.1/sub");
  });

  it("rows keep their inputs when a row above them is removed", async () => {
    const { sheet } = await openForm("add");
    const second = within(sheet).getByRole("textbox", { name: "Header 2 value" });
    await userEvent.type(second, "-beta");
    await userEvent.click(within(sheet).getByRole("button", { name: "Remove header 1" }));
    expect(within(sheet).getByRole("textbox", { name: "Header 1 value" })).toBe(second);
    expect(second).toHaveValue("v2pi/1.0-beta");
  });

  it("closing with unsaved edits asks first", async () => {
    const { sheet } = await openForm("add");
    await userEvent.type(within(sheet).getByLabelText("Name"), "draft");
    await userEvent.keyboard("{Escape}");
    const ask = await screen.findByRole("dialog", { name: "Confirm" });
    expect(ask).toHaveTextContent("Discard unsaved changes?");
    await userEvent.click(within(ask).getByRole("button", { name: "Discard" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Add subscription" })).toBeNull());
  });
});

describe("Edit subscription (U5, T6)", () => {
  it("opens with the stored values and saves them back with the changes", async () => {
    const { api$, sheet } = await openForm("work");
    const success = vi.spyOn(toast, "success");
    expect(within(sheet).getByLabelText("Name")).toHaveValue("work");
    expect(within(sheet).getByLabelText("URL")).toHaveValue(SUBS[0]!.url);
    expect(within(sheet).getByText("re-validated only when changed")).toBeInTheDocument();
    expect(within(sheet).getByLabelText("Auto-update, min")).toHaveValue(60);
    expect(within(sheet).getByRole("switch", { name: "Enabled" })).toBeChecked();
    expect(within(sheet).getByRole("textbox", { name: "Query param 1 name" })).toHaveValue("type");
    const profile = within(sheet).getByLabelText("Default tuning profile for new nodes");
    await waitFor(() => expect(within(profile).getByRole("option", { name: "fragment-tls" })).toBeInTheDocument());
    expect(within(profile).getAllByRole("option").map((option) => option.textContent)).toEqual(["(global default)", "balanced", "fragment-tls"]);

    await userEvent.clear(within(sheet).getByLabelText("Auto-update, min"));
    await userEvent.type(within(sheet).getByLabelText("Auto-update, min"), "30");
    await userEvent.click(within(sheet).getByRole("switch", { name: "Enabled" }));
    await userEvent.selectOptions(profile, "fragment-tls");
    await userEvent.clear(within(sheet).getByRole("textbox", { name: "Header 2 value" }));
    await userEvent.type(within(sheet).getByRole("textbox", { name: "Header 2 value" }), "v2pi/2.0");
    expect(within(sheet).getByText("● unsaved changes")).toBeInTheDocument();
    await userEvent.click(within(sheet).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api$.updateSub).toHaveBeenCalledWith(1, {
      name: "work", url: SUBS[0]!.url, interval_sec: 1_800,
      injection: { headers: { "x-device-os": "{device_os}", "user-agent": "v2pi/2.0" }, query: { type: "vless" } },
      enabled: false, default_profile_id: 2,
    }));
    await waitFor(() => expect(success).toHaveBeenCalledWith("Saved work", { duration: 8000 }));
  });

  it("a subscription without injected rows opens with none", async () => {
    const { sheet } = await openForm("home");
    expect(within(sheet).getByText("No headers")).toBeInTheDocument();
    await waitFor(() => expect(within(sheet).getByLabelText("Default tuning profile for new nodes")).toHaveValue("2"));
  });
});

describe("Preview and dry-run (U8, U9)", () => {
  it("Preview request shows the method, URL and headers, and changes nothing", async () => {
    const { api$, sheet, client } = await openForm("work");
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await userEvent.click(within(sheet).getByRole("button", { name: "Preview request" }));
    expect(api$.previewSub).toHaveBeenCalledWith(SUBS[0]!.url, SUBS[0]!.injection);
    expect(await within(sheet).findByLabelText("Request preview")).toHaveTextContent(
      "GET https://sub.work-vpn.example/api/v1/client/subscribe?token=9f2c7a1e&type=vless x-device-os: linux user-agent: v2pi/1.0",
    );
    expect(invalidate).not.toHaveBeenCalled();
    expect(within(sheet).queryByText("● unsaved changes")).toBeNull();
  });

  it("needs a URL first", async () => {
    const { api$, sheet } = await openForm("add");
    await userEvent.click(within(sheet).getByRole("button", { name: "Preview request" }));
    expect(await within(sheet).findByText("URL is required")).toBeInTheDocument();
    expect(api$.previewSub).not.toHaveBeenCalled();
  });

  it("Dry-run parse is busy while it fetches, then shows the count, format, truncation and nodes", async () => {
    const { api$, sheet } = await openForm("work");
    let finish: (result: PreviewNodes) => void = () => {};
    api$.previewSubNodes.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    await userEvent.click(within(sheet).getByRole("button", { name: "Dry-run parse" }));
    expect(within(sheet).getByRole("button", { name: "Parsing… (up to 20 s)" })).toBeDisabled();
    await act(async () => finish(PREVIEW_NODES));
    const result = await within(sheet).findByRole("region", { name: "Dry-run result" });
    expect(result).toHaveTextContent("Dry-run: 214 node(s) · base64/vless");
    expect(result).toHaveTextContent("Showing first 200 of 214 nodes.");
    expect(within(result).getAllByRole("row")).toHaveLength(4);
    expect(within(result).getByRole("cell", { name: "ams-edge-07" })).toBeInTheDocument();
  });

  it("an empty parse and a failed fetch say so", async () => {
    const { api$, sheet } = await openForm("work");
    api$.previewSubNodes.mockResolvedValueOnce({ format: "unknown", count: 0, returned_count: 0, truncated: false, nodes: [], skipped: {} });
    await userEvent.click(within(sheet).getByRole("button", { name: "Dry-run parse" }));
    expect(await within(sheet).findByText("No nodes parsed — check the URL, token, or format.")).toBeInTheDocument();
    api$.previewSubNodes.mockRejectedValueOnce(new ApiError(502, "fetch failed: timeout"));
    await userEvent.click(within(sheet).getByRole("button", { name: "Dry-run parse" }));
    expect(await within(sheet).findByRole("alert")).toHaveTextContent("fetch failed: timeout");
  });

  it("gives previewSubNodes more time than the default request timeout", async () => {
    const { api$, sheet } = await openForm("work");
    await userEvent.click(within(sheet).getByRole("button", { name: "Dry-run parse" }));
    await waitFor(() => expect(api$.previewSubNodes).toHaveBeenCalledWith(SUBS[0]!.url, SUBS[0]!.injection, 30_000));
  });

  it("a dry-run result no longer matching the form is hidden, not left looking current", async () => {
    const { sheet } = await openForm("work");
    await userEvent.click(within(sheet).getByRole("button", { name: "Dry-run parse" }));
    await within(sheet).findByRole("region", { name: "Dry-run result" });
    await userEvent.type(within(sheet).getByLabelText("URL"), "&extra=1");
    expect(within(sheet).queryByRole("region", { name: "Dry-run result" })).toBeNull();
    expect(within(sheet).getByText("Form changed since this run — run it again.")).toBeInTheDocument();
  });

  it("an old preview does not linger next to a validation error", async () => {
    const { sheet } = await openForm("work");
    await userEvent.click(within(sheet).getByRole("button", { name: "Preview request" }));
    await within(sheet).findByLabelText("Request preview");
    await userEvent.clear(within(sheet).getByLabelText("URL"));
    await userEvent.click(within(sheet).getByRole("button", { name: "Preview request" }));
    expect(await within(sheet).findByText("URL is required")).toBeInTheDocument();
    expect(within(sheet).queryByLabelText("Request preview")).toBeNull();
  });
});

describe("Pose as client (header presets)", () => {
  const cell = (sheet: HTMLElement, n: number, part: "name" | "value") =>
    within(sheet).getByRole("textbox", { name: `Header ${n} ${part}` });

  it("a new subscription starts as the panel itself, and a pick fills every row", async () => {
    const { sheet } = await openForm("add");
    const client = within(sheet).getByLabelText("Pose as client");
    expect(client).toHaveValue("v2pi");
    expect(sheet).toHaveTextContent("No device ID is sent");

    await userEvent.selectOptions(client, "happ-ios");

    expect(cell(sheet, 1, "value")).toHaveValue("Happ/4.6.0");
    expect(cell(sheet, 2, "name")).toHaveValue("x-hwid");
    expect(cell(sheet, 2, "value")).toHaveValue("{hwid16}");
    expect(cell(sheet, 3, "value")).toHaveValue("iOS");
    // It says what is known and what is not — the iOS user agent is documented nowhere.
    expect(sheet).toHaveTextContent("The iOS user agent is not documented anywhere");
    expect(sheet).toHaveTextContent("the TLS handshake is still the gateway's own");
    // A brand-new subscription has no device to replace, so there is nothing to warn about.
    expect(within(sheet).queryByRole("alert")).toBeNull();
  });

  it("an edited row turns the choice to Custom", async () => {
    const { sheet } = await openForm("add");
    await userEvent.selectOptions(within(sheet).getByLabelText("Pose as client"), "v2rayng");
    expect(cell(sheet, 1, "value")).toHaveValue("v2rayNG/2.2.6");

    await userEvent.type(cell(sheet, 1, "value"), "-mod");

    expect(within(sheet).getByLabelText("Pose as client")).toHaveValue("custom");
    expect(sheet).toHaveTextContent("Edited by hand");
  });

  it("the preset headers are what the request carries", async () => {
    const { api$, sheet } = await openForm("work");
    await userEvent.selectOptions(within(sheet).getByLabelText("Pose as client"), "hiddify");
    await userEvent.click(within(sheet).getByRole("button", { name: "Preview request" }));

    await waitFor(() => expect(api$.previewSub).toHaveBeenCalledWith(SUBS[0]!.url, {
      headers: { "user-agent": "HiddifyNext/4.1.1 (android) like ClashMeta v2ray sing-box", "accept-encoding": "gzip" },
      query: SUBS[0]!.injection.query,
    }));
  });

  it("on an existing subscription, a new device ID is said before it is saved", async () => {
    const { sheet } = await openForm("work");            // stored without any x-hwid
    await userEvent.selectOptions(within(sheet).getByLabelText("Pose as client"), "happ-android");

    expect(await within(sheet).findByRole("alert")).toHaveTextContent("counts it as a new one");

    // Back to what is stored: no change of device, no warning.
    await userEvent.selectOptions(within(sheet).getByLabelText("Pose as client"), "v2pi");
    expect(within(sheet).queryByRole("alert")).toBeNull();
  });

  it("a dry-run failure no longer matching the form is marked, not left looking current", async () => {
    const { api$, sheet } = await openForm("work");
    api$.previewSubNodes.mockRejectedValueOnce(new ApiError(502, "fetch failed: timeout"));
    await userEvent.click(within(sheet).getByRole("button", { name: "Dry-run parse" }));
    expect(await within(sheet).findByRole("alert")).toHaveTextContent("fetch failed: timeout");
    await userEvent.type(within(sheet).getByLabelText("URL"), "&extra=1");
    expect(within(sheet).queryByText("fetch failed: timeout")).toBeNull();
    expect(within(sheet).getByText("Form changed since this run — run it again.")).toBeInTheDocument();
  });

  it("a preview failure no longer matching the form is marked, not left looking current", async () => {
    const { api$, sheet } = await openForm("work");
    api$.previewSub.mockRejectedValueOnce(new ApiError(422, "url: not a valid http(s) URL"));
    await userEvent.click(within(sheet).getByRole("button", { name: "Preview request" }));
    expect(await within(sheet).findByRole("alert")).toHaveTextContent("url: not a valid http(s) URL");
    await userEvent.type(within(sheet).getByLabelText("URL"), "&extra=1");
    expect(within(sheet).queryByText("url: not a valid http(s) URL")).toBeNull();
    expect(within(sheet).getByText("Form changed since this run — run it again.")).toBeInTheDocument();
  });
});

