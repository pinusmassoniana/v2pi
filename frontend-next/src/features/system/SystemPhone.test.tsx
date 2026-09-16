import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import { settleConfirm } from "../../components/confirm";
import { BACKUP_DOC, LOG_LINES, RESTORE_RESULT, mockApi, mockSystem } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";
import { setViewportWidth } from "../../test/viewport";
import { clearLastRestore } from "./lastRestore";

beforeEach(() => setViewportWidth(390));
afterEach(() => {
  act(() => settleConfirm(false));
  act(() => clearLastRestore());
});

const region = (name: string) => screen.getByRole("region", { name });

function backupFile(name = "v2pi-backup-2026-09-14.json", body: unknown = BACKUP_DOC, size?: number): File {
  const file = new File([JSON.stringify(body)], name, { type: "application/json" });
  if (size !== undefined) Object.defineProperty(file, "size", { value: size });
  return file;
}

describe("Backups on a phone", () => {
  it("stacks the same cards, with the restore and the daily copy as collapsible sections", async () => {
    mockSystem(mockApi());
    renderApp("/system/backups");
    await screen.findByRole("region", { name: "Backup & restore" });

    // Create backup is one full-width button; the file-holds card is a desktop-only aside.
    expect(screen.getByRole("button", { name: "Create backup" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "What the file holds" })).toBeNull();

    const restore = screen.getByRole("button", { name: /Restore from file/ });
    expect(restore).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: /Daily auto-backup/ })).toHaveAttribute("aria-expanded", "false");
    // The section's switch sits beside its header button, never inside it: no nested controls.
    const toggle = await screen.findByRole("switch", { name: "Daily auto-backup" });
    expect(screen.getByRole("button", { name: /Daily auto-backup/ })).not.toContainElement(toggle);
  });

  it("the restore question is a sheet with the file, its checks and the sentence in one place", async () => {
    const api$ = mockSystem(mockApi());
    renderApp("/system/backups");
    await screen.findByRole("region", { name: "Backup & restore" });
    await userEvent.upload(screen.getByLabelText("Choose file…"), backupFile());
    await screen.findByText("parses as JSON, and is an object");

    await userEvent.click(screen.getByRole("button", { name: "Restore" }));

    const sheet = await screen.findByRole("dialog", { name: "Replace everything?" });
    expect(sheet).toHaveTextContent("Restore replaces every node, subscription, anti-DPI profile, routing rule and panel setting");
    expect(sheet).toHaveTextContent("It can take up to three minutes, and Connect stays blocked everywhere until it answers.");
    expect(within(sheet).getByLabelText("Change…")).toBeInTheDocument();
    expect(within(sheet).getAllByText(/under the 2 MB limit/)).not.toHaveLength(0);

    await userEvent.click(within(sheet).getByRole("button", { name: "Restore" }));

    await screen.findByText(/^restored 24 nodes/);
    expect(api$.restore).toHaveBeenCalledWith(BACKUP_DOC);
    // the outcome card uses the phone's shorter labels and its own button
    const last = await screen.findByRole("region", { name: "Last restore" });
    expect(within(last).getByText("PROFILES")).toBeInTheDocument();
    expect(within(last).getByText("disconnected")).toBeInTheDocument();
    expect(within(last).getByRole("link", { name: "Go to Home and connect" })).toBeInTheDocument();
  });

  it("the sheet stays open and shows it is running until the restore settles", async () => {
    const api$ = mockSystem(mockApi());
    renderApp("/system/backups");
    await screen.findByRole("region", { name: "Backup & restore" });
    await userEvent.upload(screen.getByLabelText("Choose file…"), backupFile());
    await userEvent.click(screen.getByRole("button", { name: "Restore" }));
    const sheet = await screen.findByRole("dialog", { name: "Replace everything?" });
    let finish: (value: typeof RESTORE_RESULT) => void = () => {};
    api$.restore.mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));

    await userEvent.click(within(sheet).getByRole("button", { name: "Restore" }));

    // Still up, and showing it: the write is in flight, not finished, and the sheet has not gone anywhere.
    await waitFor(() => expect(within(sheet).getByRole("button", { name: "Restoring…" })).toBeInTheDocument());
    expect(within(sheet).getByRole("progressbar", { name: "Restoring" })).toBeInTheDocument();
    expect(within(sheet).getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(screen.getByRole("dialog", { name: "Replace everything?" })).toBeInTheDocument();

    await act(async () => { finish(RESTORE_RESULT); });

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Replace everything?" })).toBeNull());
    await screen.findByRole("region", { name: "Last restore" });
  });

  it("Cancel in the sheet sends nothing", async () => {
    const api$ = mockSystem(mockApi());
    renderApp("/system/backups");
    await screen.findByRole("region", { name: "Backup & restore" });
    await userEvent.upload(screen.getByLabelText("Choose file…"), backupFile());
    await userEvent.click(screen.getByRole("button", { name: "Restore" }));
    const sheet = await screen.findByRole("dialog", { name: "Replace everything?" });

    await userEvent.click(within(sheet).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Replace everything?" })).toBeNull());
    expect(api$.restore).not.toHaveBeenCalled();
  });
});

describe("Logs on a phone", () => {
  async function openLogs() {
    const api$ = mockSystem(mockApi());
    const view = renderApp("/system/logs");
    await screen.findByRole("region", { name: "Logs" });
    return { api$, ...view };
  }

  it("folds the controls into one strip of chips and a ⋯ that opens the sheet", async () => {
    await openLogs();

    expect(within(region("Logs")).getAllByText("app").length).toBeGreaterThan(0);
    expect(within(region("Logs")).getByText("200 lines")).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Log source" })).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Source, lines, filter" }));

    const sheet = await screen.findByRole("dialog", { name: "Source & filter" });
    expect(within(sheet).getAllByRole("radio").map((radio) => radio.getAttribute("value"))).toEqual([
      "app", "xray-stderr", "xray-error", "xray-access",
    ]);
    expect(within(sheet).getAllByText("empty by configuration")).toHaveLength(2);
    expect(within(sheet).getByText("data/app.log")).toBeInTheDocument();
    expect(within(sheet).getByText("applied to the lines already loaded · not sent to the gateway")).toBeInTheDocument();
  });

  it("the sheet's Load reads the source it was left on and closes", async () => {
    const { api$ } = await openLogs();
    await userEvent.click(screen.getByRole("button", { name: "Source, lines, filter" }));
    const sheet = await screen.findByRole("dialog", { name: "Source & filter" });

    await userEvent.click(within(sheet).getByRole("radio", { name: /xray output/ }));
    await userEvent.click(within(sheet).getByRole("button", { name: "Load" }));

    await waitFor(() => expect(api$.getLogs).toHaveBeenCalledWith("xray-stderr", 200));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Source & filter" })).toBeNull());
    expect(within(region("Logs")).getAllByText("xray output").length).toBeGreaterThan(0);
  });

  it("truncates the timestamp and the logger name, and marks auto-refresh with a live badge", async () => {
    await openLogs();
    await userEvent.click(within(region("Logs")).getByRole("button", { name: /^(Load|Loading…)$/ }));
    await screen.findByText(/stats client reconfigured/);

    const pane = screen.getByRole("region", { name: "Log output" });
    expect(within(pane).getAllByText("…supervisor").length).toBeGreaterThan(0);
    expect(within(pane).getByText("22:10:58,003")).toBeInTheDocument();
    expect(within(pane).queryByText(LOG_LINES[0]!.slice(0, 23))).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Source, lines, filter" }));
    const sheet = await screen.findByRole("dialog", { name: "Source & filter" });
    await userEvent.click(within(sheet).getByRole("switch", { name: "Auto-refresh" }));
    await userEvent.click(within(sheet).getByRole("button", { name: "Load" }));

    expect(await within(region("Logs")).findByText("5 s")).toBeInTheDocument();
  });

  it("Download shown writes the filtered lines from inside the sheet", async () => {
    const blobs: Blob[] = [];
    URL.createObjectURL = vi.fn((blob: Blob) => { blobs.push(blob); return "blob:log"; });
    URL.revokeObjectURL = vi.fn();
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    await openLogs();
    await userEvent.click(within(region("Logs")).getByRole("button", { name: /^(Load|Loading…)$/ }));
    await screen.findByText(/stats client reconfigured/);

    await userEvent.click(screen.getByRole("button", { name: "Source, lines, filter" }));
    const sheet = await screen.findByRole("dialog", { name: "Source & filter" });
    await userEvent.type(within(sheet).getByPlaceholderText("filter…"), "ERROR");
    await userEvent.click(within(sheet).getByRole("button", { name: "Download shown" }));

    await screen.findByText("app.log downloaded · 2 lines");
    expect((await blobs[0]!.text()).split("\n")).toHaveLength(2);
    // Clear filter puts every loaded line back
    await userEvent.click(within(sheet).getByRole("button", { name: "Clear filter" }));
    expect(within(sheet).getByPlaceholderText("filter…")).toHaveValue("");
  });
});

describe("Access on a phone", () => {
  async function openAccess() {
    const api$ = mockSystem(mockApi());
    const view = renderApp("/system/access");
    await screen.findByRole("region", { name: "API tokens" });
    await screen.findByText("ci-deploy");
    return { api$, ...view };
  }

  it("renders the tokens as cards with a ⋯ menu, not a table", async () => {
    await openAccess();

    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByRole("region", { name: "Scopes" })).toBeNull();
    const rows = within(screen.getByRole("list", { name: "Tokens" })).getAllByRole("listitem");
    expect(rows).toHaveLength(3);
    expect(within(rows[1]!).getByText(/pgwp_Vt9pLs1 · created .* · last used /)).toBeInTheDocument();
    expect(within(rows[2]!).getByText("never expires")).toBeInTheDocument();
    expect(within(rows[1]!).getByRole("button", { name: "More actions for ci-deploy" })).toBeInTheDocument();
  });

  it("Revoke opens its question only once the menu has closed", async () => {
    const { api$ } = await openAccess();

    await userEvent.click(screen.getByRole("button", { name: "More actions for ci-deploy" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Revoke…" }));

    const dialog = await screen.findByRole("dialog", { name: "Confirm" });
    expect(dialog).toHaveTextContent("Revoke token “ci-deploy”? Anything using it stops working immediately.");
    await userEvent.click(within(dialog).getByRole("button", { name: "Revoke" }));
    await screen.findByText("token “ci-deploy” revoked");
    expect(api$.deleteToken).toHaveBeenCalledWith(2);
  });

  it("Create token opens a sheet whose one button is refused while the secret is on display", async () => {
    await openAccess();

    await userEvent.click(within(region("API tokens")).getByRole("button", { name: "Create token" }));
    const sheet = await screen.findByRole("dialog", { name: "New token" });
    await userEvent.type(within(sheet).getByLabelText("Name"), "home-assistant");
    await userEvent.click(within(sheet).getByRole("button", { name: "Create token" }));

    await within(sheet).findByRole("group", { name: "New token home-assistant" });
    expect(within(sheet).getByRole("button", { name: "Create token" })).toBeDisabled();
    expect(within(sheet).getByText("Finish copying the visible token first.")).toBeInTheDocument();

    await userEvent.click(within(sheet).getByRole("button", { name: "Done" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "New token" })).toBeNull());
  });

  it("closing the sheet discards what was typed, exactly as closing the desktop form does", async () => {
    await openAccess();
    const create = () => within(region("API tokens")).getByRole("button", { name: "Create token" });
    await userEvent.click(create());
    await userEvent.type(within(await screen.findByRole("dialog", { name: "New token" })).getByLabelText("Name"), "half-typed");

    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "New token" })).toBeNull());
    await userEvent.click(create());

    // The desktop form unmounts when it closes, so it starts empty; the sheet's form outlives it and
    // has to be put back by hand — otherwise it holds the unsaved-edit guard with nothing on screen.
    expect(within(await screen.findByRole("dialog", { name: "New token" })).getByLabelText("Name")).toHaveValue("");
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "New token" })).toBeNull());

    await userEvent.click(within(screen.getByRole("navigation", { name: "System tabs" })).getByRole("link", { name: "Backups" }));

    await screen.findByRole("region", { name: "Backup & restore" });
    expect(screen.queryByRole("dialog", { name: "Confirm" })).toBeNull();
  });

  it("the secret survives a backdrop tap and an Escape press; only Done clears it", async () => {
    await openAccess();
    await userEvent.click(within(region("API tokens")).getByRole("button", { name: "Create token" }));
    const sheet = await screen.findByRole("dialog", { name: "New token" });
    await userEvent.type(within(sheet).getByLabelText("Name"), "home-assistant");
    await userEvent.click(within(sheet).getByRole("button", { name: "Create token" }));
    await within(sheet).findByRole("group", { name: "New token home-assistant" });

    // Escape: incidental on a 390 px screen. The desktop cannot lose the secret this way either.
    await userEvent.keyboard("{Escape}");
    expect(screen.getByRole("dialog", { name: "New token" })).toBeInTheDocument();
    expect(within(sheet).getByRole("group", { name: "New token home-assistant" })).toBeInTheDocument();

    // A backdrop tap: Radix defers an outside pointerdown to the click that follows it.
    fireEvent.pointerDown(document.body, { button: 0 });
    fireEvent.click(document.body);
    expect(screen.getByRole("dialog", { name: "New token" })).toBeInTheDocument();
    expect(within(sheet).getByRole("group", { name: "New token home-assistant" })).toBeInTheDocument();

    // Done still works: an explicit dismissal is not what either guard refuses.
    await userEvent.click(within(sheet).getByRole("button", { name: "Done" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "New token" })).toBeNull());
  });

  it("the password question says this browser survives it", async () => {
    await openAccess();
    expect(within(region("Session")).getByText("saved on change")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Current password"), "old-password");
    await userEvent.type(screen.getByLabelText("New password"), "New-passw0rd!");
    await userEvent.type(screen.getByLabelText("Confirm new password"), "New-passw0rd!");

    await userEvent.click(within(region("Password")).getByRole("button", { name: "Change password" }));

    expect(await screen.findByRole("dialog", { name: "Confirm" })).toHaveTextContent("This browser stays signed in.");
  });

  it("the audit log is one card per row, with the same masking", async () => {
    await openAccess();
    const audit = region("Audit log");

    await userEvent.click(within(audit).getByRole("button", { name: "Show" }));

    const rows = within(await within(audit).findByRole("list", { name: "Audit entries" })).getAllByRole("listitem");
    expect(rows).toHaveLength(8);
    expect(within(rows[0]!).getByText("/api/rw/clients/3f2a1c4e…")).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain("3f2a1c4e-77b0");
  });
});

describe("Panel on a phone", () => {
  async function openPanel() {
    const api$ = mockSystem(mockApi());
    const view = renderApp("/system/panel");
    await screen.findByRole("region", { name: "System" });
    return { api$, ...view };
  }

  it("is the diagnostics card and three collapsible sections", async () => {
    await openPanel();

    expect(within(region("System")).getByText("30 s")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Traffic stats" })).toBeNull();
    expect(screen.getByRole("button", { name: /Traffic stats/ })).toHaveAttribute("aria-expanded", "true");
    for (const name of [/Settings file/, /Danger zone/]) {
      expect(screen.getByRole("button", { name })).toHaveAttribute("aria-expanded", "false");
    }
  });

  it("the sticky footer appears only while the stats form is dirty, and Save is in it", async () => {
    const { api$ } = await openPanel();
    const interval = () => screen.getByLabelText("Sample interval") as HTMLInputElement;
    await waitFor(() => expect(interval()).toHaveValue(1000));
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();

    await userEvent.clear(interval());
    await userEvent.click(interval());
    await userEvent.paste("2000");

    const save = await screen.findByRole("button", { name: "Save" });
    expect(await screen.findByText("● unsaved")).toBeInTheDocument();
    await userEvent.click(save);

    await screen.findByText(/^traffic stats|^saved/);
    expect(api$.putSettings).toHaveBeenCalledWith({ traffic_sample_ms: 2000 });
    await waitFor(() => expect(screen.queryByRole("button", { name: "Save" })).toBeNull());
  });

  it("the danger zone keeps its condensed chips and the same question", async () => {
    await openPanel();
    await userEvent.click(screen.getByRole("button", { name: /Danger zone/ }));

    const zone = screen.getByRole("button", { name: /Danger zone/ }).closest("fieldset")!;
    expect(within(zone).getAllByRole("listitem")).toHaveLength(9);
    await userEvent.click(within(zone).getByRole("button", { name: "Reset settings" }));
    expect(await screen.findByRole("dialog", { name: "Confirm" })).toHaveTextContent("turns gateway DNS over DoH off");
  });

  it("a save's outcome stays visible once the footer disappears, because it never lived inside it", async () => {
    const { api$ } = await openPanel();
    const interval = () => screen.getByLabelText("Sample interval") as HTMLInputElement;
    await waitFor(() => expect(interval()).toHaveValue(1000));
    const stats = screen.getByRole("button", { name: /Traffic stats/ }).closest("fieldset")!;

    await userEvent.clear(interval());
    await userEvent.click(interval());
    await userEvent.paste("2000");
    await userEvent.click(await screen.findByRole("button", { name: "Save" }));

    await waitFor(() => expect(screen.queryByRole("button", { name: "Save" })).toBeNull());
    // The footer (Discard/Save) is gone now that the form is clean again — the outcome line is not.
    expect(within(stats).getByRole("status")).toHaveTextContent("✓ saved");
    expect(api$.putSettings).toHaveBeenCalledWith({ traffic_sample_ms: 2000 });
  });

  it("a 502 rollback also leaves its outcome visible, not just toasted", async () => {
    const api$ = mockSystem(mockApi());
    api$.putSettings.mockRejectedValueOnce(new ApiError(502, "xray -test rejected the rebuilt config"));
    renderApp("/system/panel");
    await screen.findByRole("region", { name: "System" });
    const interval = () => screen.getByLabelText("Sample interval") as HTMLInputElement;
    await waitFor(() => expect(interval()).toHaveValue(1000));
    const stats = screen.getByRole("button", { name: /Traffic stats/ }).closest("fieldset")!;

    await userEvent.clear(interval());
    await userEvent.click(interval());
    await userEvent.paste("2000");
    await userEvent.click(await screen.findByRole("button", { name: "Save" }));

    await waitFor(() => expect(screen.queryByRole("button", { name: "Save" })).toBeNull());
    expect(within(stats).getByRole("status")).toHaveTextContent("✕ not saved");
  });

  it("a section holding an invalid field cannot be hidden by opening another one", async () => {
    await openPanel();
    const interval = () => screen.getByLabelText("Sample interval") as HTMLInputElement;
    await waitFor(() => expect(interval()).toHaveValue(1000));
    const stats = screen.getByRole("button", { name: /Traffic stats/ }).closest("fieldset")!;

    await userEvent.clear(interval());
    await userEvent.click(interval());
    await userEvent.paste("100");
    await waitFor(() => expect(within(stats).getByRole("status")).toHaveTextContent("1 field invalid · fix them to save"));

    await userEvent.click(screen.getByRole("button", { name: /Settings file/ }));

    expect(screen.getByRole("button", { name: /Traffic stats/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: /Settings file/ })).toHaveAttribute("aria-expanded", "true");
  });
});
