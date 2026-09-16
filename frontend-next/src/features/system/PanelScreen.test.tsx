import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type Settings, type Status } from "../../api/client";
import { SETTINGS_CONNECTION_WRITE, SETTINGS_WRITE } from "../../api/invalidation";
import { settleConfirm } from "../../components/confirm";
import { DIAGNOSTICS, SETTINGS, STATUS, holdConnectionWrite, mockApi, mockSystem } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";
import { exportSettings } from "./settingsFile";

// Two tests in this file save an interval-only patch that produces the same "saved — applies on next
// Connect" text (as toaster.test.tsx:8 does for the same reason): sonner's toast store is a module
// singleton whose dismiss timer only runs while a Toaster is mounted, so an un-dismissed toast from an
// earlier test is replayed into the next test's fresh Toaster and collides with an identical message.
afterEach(() => act(() => { settleConfirm(false); toast.dismiss(); }));

async function openPanel(options: { settings?: Partial<Settings>; status?: Partial<Status> } = {}) {
  const api$ = mockSystem(mockApi());
  let stored: Settings = { ...SETTINGS, ...options.settings };
  api$.getSettings.mockImplementation(async () => stored);
  api$.putSettings.mockImplementation(async (patch: Partial<Settings>) => { stored = { ...stored, ...patch }; return stored; });
  if (options.status) api$.getStatus.mockResolvedValue({ ...STATUS, ...options.status });
  const view = renderApp("/system/panel");
  await screen.findByRole("region", { name: "Traffic stats" });
  return { api$, ...view };
}

const region = (name: string) => screen.getByRole("region", { name });
const field = (label: string) => screen.getByLabelText(label) as HTMLInputElement;
const saveButton = () => within(region("Traffic stats")).getByRole("button", { name: /^(Save|Saving…)$/ });

async function fill(input: HTMLElement, value: string) {
  await userEvent.clear(input);
  await userEvent.click(input);
  await userEvent.paste(value);
}

async function answer(label: string) {
  const dialog = await screen.findByRole("dialog", { name: "Confirm" });
  await userEvent.click(within(dialog).getByRole("button", { name: label }));
}

describe("Panel — diagnostics (P3)", () => {
  it("shows the six tiles through the shared formatters, with the database path muted", async () => {
    await openPanel();
    const card = await screen.findByRole("region", { name: "System" });
    await within(card).findByText(DIAGNOSTICS.app_version);

    expect(within(card).getAllByRole("term").map((cell) => cell.textContent)).toEqual([
      "APP VERSION", "XRAY-CORE", "PANEL UPTIME", "DATABASE", "DISK FREE", "DISK TOTAL",
    ]);
    expect(within(card).getByText("25.3.6")).toHaveClass("font-mono");
    expect(within(card).getByText("6d 4h")).toBeInTheDocument();
    expect(within(card).getByText("2.1 MB")).toBeInTheDocument();
    expect(within(card).getByText("11.40 GB")).toBeInTheDocument();
    expect(card).toHaveTextContent("Uptime is the panel process, not the host and not the tunnel.");
    expect(within(card).getByText(DIAGNOSTICS.db_path)).toHaveClass("font-mono");
  });

  it("“unavailable” is a state, not a version, and Refresh re-reads once", async () => {
    const { api$ } = await openPanel();
    api$.getDiagnostics.mockResolvedValue({ ...DIAGNOSTICS, xray_version: "unavailable" });
    const card = await screen.findByRole("region", { name: "System" });

    await userEvent.click(within(card).getByRole("button", { name: "Refresh" }));

    const state = await within(card).findByText("unavailable");
    expect(state).toHaveClass("text-t3");
    expect(within(card).getByText("the panel could not run `xray -version`")).toBeInTheDocument();
    expect(api$.getDiagnostics).toHaveBeenCalledTimes(2);
  });

  it("with nothing to show it says so, with a retry", async () => {
    const api$ = mockSystem(mockApi());
    api$.getDiagnostics.mockRejectedValue(new ApiError(500, "diagnostics failed"));
    renderApp("/system/panel");
    expect(await screen.findByText("diagnostics unavailable", {}, { timeout: 3000 })).toBeInTheDocument();
  });
});

describe("Panel — traffic stats (G2)", () => {
  it("refuses out-of-range values in the gateway's own words and will not save while any is wrong", async () => {
    await openPanel();
    await waitFor(() => expect(field("Sample interval")).toHaveValue(1000));

    await fill(field("Sample interval"), "250");
    await fill(field("Xray API port"), "52345");

    expect(await screen.findByText("traffic_sample_ms must be >= 500")).toBeInTheDocument();
    expect(screen.getByText("stats_api_port collides with a system port")).toBeInTheDocument();
    await waitFor(() => expect(saveButton()).toBeDisabled());
    expect(within(region("Traffic stats")).getByRole("status")).toHaveTextContent("2 fields invalid · fix them to save");
  });

  it("a sample-interval-only save sends one field as a plain settings write", async () => {
    const { api$, client } = await openPanel({ status: { active_node_id: null } });
    await waitFor(() => expect(field("Sample interval")).toHaveValue(1000));
    const keys$: unknown[] = [];
    client.getMutationCache().subscribe((event) => { if (event.type === "added") keys$.push(event.mutation.options.mutationKey); });

    await fill(field("Sample interval"), "2000");
    await waitFor(() => expect(saveButton()).toBeEnabled());
    await userEvent.click(saveButton());

    await screen.findByText("saved — applies on next Connect");
    expect(api$.putSettings).toHaveBeenCalledWith({ traffic_sample_ms: 2000 });
    expect(keys$[0]).toEqual([...SETTINGS_WRITE]);
    // no question: this patch re-applies nothing
    expect(screen.queryByRole("dialog", { name: "Confirm" })).toBeNull();
  });

  it("a port change is a connection write, and says it reached the live tunnel", async () => {
    const { api$, client } = await openPanel();
    await waitFor(() => expect(field("Xray API port")).toHaveValue(10085));
    const keys$: unknown[] = [];
    client.getMutationCache().subscribe((event) => { if (event.type === "added") keys$.push(event.mutation.options.mutationKey); });

    await fill(field("Xray API port"), "10086");
    await waitFor(() => expect(saveButton()).toBeEnabled());
    await userEvent.click(saveButton());

    await screen.findByText("saved · applied to the live tunnel");
    expect(api$.putSettings).toHaveBeenCalledWith({ stats_api_port: 10086 });
    expect(keys$[0]).toEqual([...SETTINGS_CONNECTION_WRITE]);
  });

  it("asks before a re-applying save would start an xray that was stopped with a node still selected", async () => {
    const { api$ } = await openPanel({ status: { running: false, xray_state: "stopped", active_node_id: 1 } });
    await waitFor(() => expect(field("Sample interval")).toHaveValue(1000));

    // an interval-only save re-applies nothing, so it never asks
    await fill(field("Sample interval"), "2000");
    await waitFor(() => expect(saveButton()).toBeEnabled());
    await userEvent.click(saveButton());
    await screen.findByText("saved — applies on next Connect");
    expect(screen.queryByRole("dialog", { name: "Confirm" })).toBeNull();

    await userEvent.click(screen.getByRole("switch", { name: "Collect traffic stats" }));
    await waitFor(() => expect(saveButton()).toBeEnabled());
    await userEvent.click(saveButton());
    expect(await screen.findByRole("dialog", { name: "Confirm" })).toHaveTextContent("This starts the tunnel again. Continue?");
    await answer("Cancel");
    expect(api$.putSettings).toHaveBeenCalledTimes(1);
  });

  it("nothing is sent when a connection write started while the question was open", async () => {
    const { api$, client } = await openPanel({ status: { running: false, xray_state: "stopped", active_node_id: 1 } });
    await waitFor(() => expect(field("Xray API port")).toHaveValue(10085));
    await fill(field("Xray API port"), "10086");
    await waitFor(() => expect(saveButton()).toBeEnabled());
    await userEvent.click(saveButton());
    const release = holdConnectionWrite(client);
    await answer("Continue");

    await screen.findByText("Another connection change is still running — try again when it finishes");
    expect(api$.putSettings).not.toHaveBeenCalled();
    await release();
  });

  it("a 502 puts the whole card back, because the transaction rolled it back", async () => {
    const { api$ } = await openPanel({ status: { active_node_id: 1, xray_state: "working", running: true } });
    api$.putSettings.mockRejectedValueOnce(new ApiError(502, "xray -test rejected the rebuilt config"));
    await waitFor(() => expect(field("Xray API port")).toHaveValue(10085));

    await fill(field("Xray API port"), "10086");
    await fill(field("Sample interval"), "2000");
    await waitFor(() => expect(saveButton()).toBeEnabled());
    await userEvent.click(saveButton());

    await screen.findByText("not saved — applying to the tunnel failed: xray -test rejected the rebuilt config");
    await waitFor(() => expect(field("Xray API port")).toHaveValue(10085));
    expect(field("Sample interval")).toHaveValue(1000);
  });

  it("Discard puts the form back and the unsaved mark goes with it", async () => {
    await openPanel();
    await waitFor(() => expect(field("Sample interval")).toHaveValue(1000));
    await fill(field("Sample interval"), "2000");

    expect(await screen.findByText("● unsaved changes")).toBeInTheDocument();
    await userEvent.click(within(region("Traffic stats")).getByRole("button", { name: "Discard" }));

    await waitFor(() => expect(field("Sample interval")).toHaveValue(1000));
    expect(screen.queryByText("● unsaved changes")).toBeNull();
  });

  it("warns, on this card and nowhere else, when the collector has never reached xray", async () => {
    const api$ = mockSystem(mockApi());
    api$.getDiagnostics.mockResolvedValue({ ...DIAGNOSTICS, stats_last_ok_at: null, stats_fail_count: 9, stats_error: "connection refused" });
    renderApp("/system/panel");

    const warning = await screen.findByRole("group", { name: "The panel cannot reach xray's stats API on port 10085" });
    expect(within(region("Traffic stats"))).toBeTruthy();
    expect(region("Traffic stats")).toContainElement(warning);
    expect(warning).toHaveTextContent("connection refused. This is why the Home graph is flat");
    expect(within(await screen.findByRole("region", { name: "System" })).queryByText(/stats API/)).toBeNull();
  });

  it("a healthy collector reports itself instead", async () => {
    await openPanel();
    expect(await within(region("Traffic stats")).findByText("COLLECTOR OK")).toBeInTheDocument();
    // Task 6 review correction (integration-notes.md, Task 6 → Task 11): stats_fail_count resets on every
    // success and on reconfigure (backend/pi_gw_panel/stats/client.py:58-70), so the copy reads "consecutive
    // failures since the last success", never "since start".
    expect(region("Traffic stats")).toHaveTextContent("0 consecutive failures since the last success");
  });
});

describe("Panel — settings file (G6)", () => {
  it("exports what the panel already has, without the routing-owned key and without a request", async () => {
    const blobs: Blob[] = [];
    const names: string[] = [];
    URL.createObjectURL = vi.fn((blob: Blob) => { blobs.push(blob); return "blob:settings"; });
    URL.revokeObjectURL = vi.fn();
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function mock(this: HTMLAnchorElement) { names.push(this.download); });
    const { api$ } = await openPanel();
    const before = api$.getSettings.mock.calls.length;

    await userEvent.click(screen.getByRole("button", { name: "Export settings" }));

    await screen.findByText("settings exported · v2pi-settings.json");
    expect(names[0]).toBe("v2pi-settings.json");
    const doc = JSON.parse(await blobs[0]!.text()) as Record<string, unknown>;
    expect(doc).toEqual(exportSettings(SETTINGS));
    expect(doc).not.toHaveProperty("routing_default_action");
    expect(api$.getSettings.mock.calls.length).toBe(before);
  });

  it("names the unknown fields before anything is sent", async () => {
    const { api$ } = await openPanel();
    const file = new File([JSON.stringify({ stats_enabled: true, health_probe_urls: "x", rw_enabled: "1" })], "settings-old-box.json", { type: "application/json" });

    await userEvent.upload(within(region("Settings file")).getByLabelText("Choose file…"), file);

    expect(await screen.findByText("2 fields are not settings keys: health_probe_urls, rw_enabled")).toBeInTheDocument();
    expect(screen.getByText("Remove them, or export a fresh file from this panel — one unknown key refuses the whole import.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Import settings" })).toBeDisabled();
    expect(api$.putSettings).not.toHaveBeenCalled();
  });

  it("asks before applying a file, and sends only what it carries", async () => {
    const { api$ } = await openPanel();
    const file = new File([JSON.stringify({ health_interval: 1800, session_timeout_min: 0 })], "v2pi-settings.json", { type: "application/json" });
    await userEvent.upload(within(region("Settings file")).getByLabelText("Choose file…"), file);
    await screen.findByText("2 fields, all known settings keys");
    expect(screen.getByText("none of them rebuild the live tunnel")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Import settings" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm" });
    expect(dialog).toHaveTextContent("Apply the settings in this file? Only the fields it contains are changed; the rest stay as they are. Unknown fields are refused. This can rebuild the live tunnel.");
    expect(within(dialog).getByRole("button", { name: "Import settings" })).not.toHaveClass("text-bad");
    await answer("Import settings");

    await screen.findByText("settings applied from file");
    expect(api$.putSettings).toHaveBeenCalledWith({ health_interval: 1800, session_timeout_min: 0 });
  });

  it("adds the stopped-xray line only when the file would rebuild the tunnel", async () => {
    await openPanel({ status: { running: false, xray_state: "stopped", active_node_id: 1 } });
    const picker = () => within(region("Settings file")).getByLabelText(/^(Choose file…|Change…)$/);

    await userEvent.upload(picker(), new File([JSON.stringify({ health_interval: 1800 })], "a.json", { type: "application/json" }));
    await screen.findByText("1 fields, all known settings keys");
    await userEvent.click(screen.getByRole("button", { name: "Import settings" }));
    expect(await screen.findByRole("dialog", { name: "Confirm" })).not.toHaveTextContent("This will also start xray");
    await answer("Cancel");

    await userEvent.upload(picker(), new File([JSON.stringify({ dns_intercept: true })], "b.json", { type: "application/json" }));
    await screen.findByText("1 of them rebuild the live tunnel: dns_intercept");
    await userEvent.click(screen.getByRole("button", { name: "Import settings" }));
    expect(await screen.findByRole("dialog", { name: "Confirm" })).toHaveTextContent("This will also start xray, which is currently stopped.");
  });

  it("a 422 and a 502 each say what did not happen", async () => {
    const { api$ } = await openPanel();
    const pick = async () => {
      await userEvent.upload(within(region("Settings file")).getByLabelText(/^(Choose file…|Change…)$/), new File([JSON.stringify({ traffic_sample_ms: 1000 })], "s.json", { type: "application/json" }));
      await screen.findByText("1 fields, all known settings keys");
      await userEvent.click(screen.getByRole("button", { name: "Import settings" }));
      await answer("Import settings");
    };

    api$.putSettings.mockRejectedValueOnce(new ApiError(422, "traffic_sample_ms must be >= 500"));
    await pick();
    await screen.findByText("not applied — traffic_sample_ms must be >= 500");

    api$.putSettings.mockRejectedValueOnce(new ApiError(502, "xray -test rejected the rebuilt config"));
    await pick();
    await screen.findByText("not applied — applying to the tunnel failed: xray -test rejected the rebuilt config");
  });
});

describe("Panel — danger zone (P5)", () => {
  it("lists the sixteen keys it writes, with their defaults", async () => {
    await openPanel();
    const zone = region("Danger zone");
    expect(within(zone).getAllByRole("listitem")).toHaveLength(16);
    expect(within(zone).getByText("tunneled_fetch 1")).toBeInTheDocument();
    expect(within(zone).getByText("stats_api_port 10085")).toBeInTheDocument();
    expect(within(zone).getByText("auto_backup_enabled 0")).toBeInTheDocument();
    expect(within(zone).queryByText(/routing_default_action/)).toBeNull();
    expect(zone).toHaveTextContent("The reset reaches Subscriptions and Gateway too, and it rebuilds the live tunnel.");
  });

  it("names every section it touches, and says it reached the live tunnel", async () => {
    const { api$ } = await openPanel({ status: { active_node_id: 1, xray_state: "working", running: true } });

    await userEvent.click(within(region("Danger zone")).getByRole("button", { name: "Reset settings" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm" });
    expect(dialog).toHaveTextContent("turns subscription auto-switch and tunnelled subscription fetch back on");
    expect(dialog).toHaveTextContent("turns gateway DNS over DoH off");
    expect(dialog).toHaveTextContent("Nodes, subscriptions, anti-DPI profiles and routing rules are kept.");
    expect(dialog).not.toHaveTextContent("This will also start xray");
    await answer("Reset settings");

    await screen.findByText("settings reset to defaults · applied to the live tunnel");
    expect(api$.resetSettings).toHaveBeenCalledTimes(1);
  });

  it("with nothing connected it says the reset applies on the next Connect; with a stopped xray it warns first", async () => {
    await openPanel({ status: { active_node_id: null } });
    await userEvent.click(within(region("Danger zone")).getByRole("button", { name: "Reset settings" }));
    await answer("Reset settings");
    await screen.findByText("settings reset to defaults — applies on next Connect");
  });

  it("a 502 says the whole reset was rolled back", async () => {
    const { api$ } = await openPanel();
    api$.resetSettings.mockRejectedValueOnce(new ApiError(502, "xray -test rejected the rebuilt config"));

    await userEvent.click(within(region("Danger zone")).getByRole("button", { name: "Reset settings" }));
    await answer("Reset settings");

    await screen.findByText("not reset — applying to the tunnel failed: xray -test rejected the rebuilt config");
  });
});
