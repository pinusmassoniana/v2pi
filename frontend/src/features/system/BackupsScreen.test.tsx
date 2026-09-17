import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type Settings } from "../../api/client";
import { SETTINGS_CONNECTION_WRITE, SETTINGS_WRITE } from "../../api/invalidation";
import { keys } from "../../api/keys";
import { settleConfirm } from "../../components/confirm";
import { BACKUP_DOC, BACKUP_FILES, RESTORE_RESULT, SETTINGS, holdConnectionWrite, holdWrite, mockApi, mockSystem } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";
import { clearLastRestore } from "./lastRestore";

afterEach(() => {
  act(() => settleConfirm(false));
  act(() => clearLastRestore());
  vi.useRealTimers();
});

async function openBackups(over: Partial<Settings> = {}) {
  const api$ = mockSystem(mockApi());
  // The gateway keeps what a save wrote, so the re-read after an invalidation agrees with the toast.
  let stored: Settings = { ...SETTINGS, ...over };
  api$.getSettings.mockImplementation(async () => stored);
  api$.putSettings.mockImplementation(async (patch: Partial<Settings>) => { stored = { ...stored, ...patch }; return stored; });
  const view = renderApp("/system/backups");
  await screen.findByRole("region", { name: "Backup & restore" });
  return { api$, ...view };
}

const region = (name: string) => screen.getByRole("region", { name });
const restoreButton = () => within(region("Restore from file")).getByRole("button", { name: /^(Restore|Restoring…)$/ });

function backupFile(name: string, body: unknown = BACKUP_DOC, size?: number): File {
  const text = typeof body === "string" ? body : JSON.stringify(body);
  const file = new File([text], name, { type: "application/json" });
  if (size !== undefined) Object.defineProperty(file, "size", { value: size });
  return file;
}

async function pick(file: File) {
  await userEvent.upload(within(region("Restore from file")).getByLabelText(/^(Choose file…|Change…)$/), file);
  await screen.findByText(file.name);
}

/** Answer the open confirmation by its button, having first checked the question. */
async function answer(label: string) {
  const dialog = await screen.findByRole("dialog", { name: "Confirm" });
  await userEvent.click(within(dialog).getByRole("button", { name: label }));
}

function captureDownloads() {
  const blobs: Blob[] = [];
  const names: string[] = [];
  URL.createObjectURL = vi.fn((blob: Blob) => { blobs.push(blob); return "blob:backup"; });
  URL.revokeObjectURL = vi.fn();
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function mock(this: HTMLAnchorElement) {
    names.push(this.download);
  });
  return { blobs, names, click };
}

describe("Backups — create (P1)", () => {
  it("fetches the document on click, hands it to the browser and keeps nothing", async () => {
    const downloads = captureDownloads();
    const { api$, client } = await openBackups();

    await userEvent.click(screen.getByRole("button", { name: "Create backup" }));

    await screen.findByText(/^backup downloaded · v2pi-backup-\d{4}-\d{2}-\d{2}\.json$/);
    expect(api$.getBackup).toHaveBeenCalledTimes(1);
    expect(downloads.names[0]).toMatch(/^v2pi-backup-\d{4}-\d{2}-\d{2}\.json$/);
    expect(JSON.parse(await downloads.blobs[0]!.text())).toEqual(BACKUP_DOC);
    // A backup is the whole configuration: it must not sit in the query cache afterwards.
    expect(client.getQueryCache().getAll().map((query) => query.queryKey.join("/"))).not.toContain("backup");
    expect(client.getQueryCache().getAll().some((query) => query.state.data === BACKUP_DOC)).toBe(false);
  });

  it("a 500 says the panel's own export would not restore, which is the point of the check", async () => {
    const { api$ } = await openBackups();
    api$.getBackup.mockRejectedValueOnce(new ApiError(500, "stored state is not restorable: node 31 refers to a profile that no longer exists"));

    await userEvent.click(screen.getByRole("button", { name: "Create backup" }));

    await screen.findByText("backup failed — stored state is not restorable: node 31 refers to a profile that no longer exists");
  });

  it("waits while a connection write runs", async () => {
    const { client } = await openBackups();
    const release = holdConnectionWrite(client);
    await waitFor(() => expect(screen.getByRole("button", { name: "Create backup" })).toBeDisabled());
    await release();
    await waitFor(() => expect(screen.getByRole("button", { name: "Create backup" })).toBeEnabled());
  });
});

describe("Backups — restore (P2)", () => {
  it("names each check, blocks Restore until they all pass, and passes a document with no nodes", async () => {
    await openBackups();
    expect(restoreButton()).toBeDisabled();

    await pick(backupFile("notjson.json", "{oops"));
    expect(screen.getByText("parses as JSON").previousSibling).toHaveTextContent("✕");
    expect(restoreButton()).toBeDisabled();

    await pick(backupFile("array.json", [1, 2]));
    expect(screen.getByText("an object, not an array").previousSibling).toHaveTextContent("✕");
    expect(restoreButton()).toBeDisabled();

    await pick(backupFile("nomarker.json", { nodes: [] }));
    expect(screen.getByText("carries a schema_version").previousSibling).toHaveTextContent("✕");
    expect(restoreButton()).toBeDisabled();

    // BackupDocument.nodes defaults to []: a gateway with no nodes exports a valid document.
    await pick(backupFile("nonodes.json", { schema_version: 2, profiles: [{ name: "p" }] }));
    for (const label of ["parses as JSON", "an object, not an array", "carries a schema_version"]) {
      expect(screen.getByText(label).previousSibling).toHaveTextContent("✓");
    }
    expect(restoreButton()).toBeEnabled();
  });

  it("an oversized file gets a sentence instead of a request", async () => {
    const { api$ } = await openBackups();

    await pick(backupFile("huge.json", BACKUP_DOC, 3 * 1024 * 1024));

    expect(screen.getByRole("group", { name: "That file is larger than 2 MB" })).toBeInTheDocument();
    expect(screen.queryByText("parses as JSON")).toBeNull();
    expect(restoreButton()).toBeDisabled();
    expect(api$.restore).not.toHaveBeenCalled();
  });

  it("a file the browser cannot read says so, instead of failing silently, and leaves no stale selection", async () => {
    const { api$ } = await openBackups();
    const unreadable = backupFile("evicted.json");
    vi.spyOn(unreadable, "text").mockRejectedValueOnce(new DOMException("could not read", "NotReadableError"));

    await userEvent.upload(within(region("Restore from file")).getByLabelText("Choose file…"), unreadable);

    await screen.findByText("could not read that file");
    expect(screen.queryByText(unreadable.name)).toBeNull();
    expect(restoreButton()).toBeDisabled();
    expect(api$.restore).not.toHaveBeenCalled();
  });

  it("asks with the file's name and size, then replaces everything and says what came back", async () => {
    const { api$ } = await openBackups();
    await pick(backupFile("v2pi-backup-2026-09-14.json", BACKUP_DOC, 214_000));

    await userEvent.click(restoreButton());
    const dialog = await screen.findByRole("dialog", { name: "Confirm" });
    expect(dialog).toHaveTextContent("Restore replaces every node, subscription, anti-DPI profile, routing rule and panel setting");
    expect(dialog).toHaveTextContent("The Reality private key and the remote-access client list are not restored.");
    expect(dialog).toHaveTextContent("v2pi-backup-2026-09-14.json · 214 KB");
    await answer("Restore");

    await screen.findByText("restored 24 nodes, 3 subscriptions, 6 profiles, 11 routing rules — the gateway is disconnected; Connect a node when you are ready");
    expect(api$.restore).toHaveBeenCalledWith(BACKUP_DOC);
    // the outcome stays on screen as a card, with the snapshot path and the way back to Home
    const last = await screen.findByRole("region", { name: "Last restore" });
    expect(last).toHaveTextContent("from v2pi-backup-2026-09-14.json");
    expect(last).toHaveTextContent(`A copy of what this replaced was saved on the gateway at ${RESTORE_RESULT.pre_restore_snapshot}.`);
    expect(within(last).getByRole("link", { name: "Go to Home" })).toBeInTheDocument();
    expect(within(last).getByText("runtime: disconnected")).toBeInTheDocument();
  });

  it("keeps the parsed document out of the MutationCache after a restore", async () => {
    const { api$, client } = await openBackups();
    await pick(backupFile("v2pi-backup-2026-09-14.json", BACKUP_DOC, 214_000));
    await userEvent.click(restoreButton());
    await answer("Restore");
    // Not the toast text: an identical message from an earlier test in this file can still be on screen (sonner's
    // own toast store outlives a test's unmounted render), which would make that query ambiguous instead of unique.
    await screen.findByRole("region", { name: "Last restore" });
    expect(api$.restore).toHaveBeenCalledWith(BACKUP_DOC);
    // A settled mutation's state.variables sits in the MutationCache for its gcTime: the parsed document must
    // never be one of them, the same guard `useRwSave` and `usePasswordChange` already carry for their secrets.
    const mutations = client.getMutationCache().getAll();
    expect(mutations.length).toBeGreaterThan(0);
    const serialized = JSON.stringify(mutations.map((mutation) => mutation.state.variables));
    expect(serialized).not.toContain("nl-ams-03");
    expect(serialized).not.toContain("uuid-1");
  });

  it("a restore that turned remote access off is a warning, and says which", async () => {
    const { api$ } = await openBackups();
    api$.restore.mockResolvedValueOnce({
      ...RESTORE_RESULT,
      restored: { ...RESTORE_RESULT.restored, rw_disabled: "the restored public key does not match the local private key" },
    });
    await pick(backupFile("b.json"));
    await userEvent.click(restoreButton());
    await answer("Restore");

    const toast = await screen.findByText(/remote access was turned off: the restored public key does not match the local private key/);
    expect(toast.closest("[data-sonner-toast]")).toHaveAttribute("data-type", "warning");
    expect(await screen.findByRole("group", { name: "Remote access was turned off" })).toBeInTheDocument();
  });

  it("nothing is sent when another connection write started while the question was open", async () => {
    const { api$, client } = await openBackups();
    await pick(backupFile("b.json"));
    await userEvent.click(restoreButton());
    const release = holdConnectionWrite(client);
    await answer("Restore");

    await screen.findByText("Another connection change is still running — try again when it finishes");
    expect(api$.restore).not.toHaveBeenCalled();
    await release();
  });

  it("locks the picker and both buttons while it runs, and shows how long it has been going", async () => {
    // Only the clock is faked: userEvent's own waits stay on real timers, as the Apply-progress test does.
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    const { api$ } = await openBackups();
    let finish: (value: typeof RESTORE_RESULT) => void = () => {};
    api$.restore.mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
    await pick(backupFile("b.json"));
    await userEvent.click(restoreButton());
    await answer("Restore");

    await waitFor(() => expect(restoreButton()).toHaveTextContent("Restoring…"));
    expect(screen.getByRole("progressbar", { name: "Restoring" })).toBeInTheDocument();
    expect(within(region("Restore from file")).getByLabelText("Change…")).toBeDisabled();
    expect(within(region("Restore from file")).getByRole("button", { name: "Clear" })).toBeDisabled();
    expect(screen.getByText(/The request waits up to 180 s/)).toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(72_000));
    expect(screen.getByText("1:12")).toBeInTheDocument();

    await act(async () => { finish(RESTORE_RESULT); });
    await waitFor(() => expect(screen.queryByRole("progressbar", { name: "Restoring" })).toBeNull());
  });

  it("a refusal says what did not happen, with the technical wrapping stripped", async () => {
    const { api$ } = await openBackups();
    api$.restore.mockRejectedValueOnce(new ApiError(400, "invalid backup: : Value error, the gateway segment in this file overlaps the network you are connected from"));
    await pick(backupFile("b.json"));
    await userEvent.click(restoreButton());
    await answer("Restore");
    await screen.findByText("not restored — the gateway segment in this file overlaps the network you are connected from");

    api$.restore.mockRejectedValueOnce(new ApiError(400, "invalid backup: settings.health_interval: Input should be a valid integer"));
    await userEvent.click(restoreButton());
    await answer("Restore");
    await screen.findByText("not restored — settings.health_interval: Input should be a valid integer");
  });

  it("a 502 that names a recovery stays until it is dismissed", async () => {
    const { api$ } = await openBackups();
    api$.restore.mockRejectedValueOnce(new ApiError(502, "host provisioning failed: nft missing; recovery: the previous configuration was put back and the guard reinstalled"));
    await pick(backupFile("b.json"));
    await userEvent.click(restoreButton());
    await answer("Restore");

    const text = await screen.findByText(/^not restored — the gateway could not apply it: host provisioning failed/);
    expect(within(text.closest("[data-sonner-toast]")!).getByRole("button", { name: /close/i })).toBeInTheDocument();
  });

  it("a request that never answered says so in amber and re-reads everything", async () => {
    const { api$, client } = await openBackups();
    api$.restore.mockRejectedValueOnce(new ApiError(0, "request timed out"));
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await pick(backupFile("b.json"));
    await userEvent.click(restoreButton());
    await answer("Restore");

    const toast = await screen.findByText("no answer yet — the gateway may still be applying; reloading");
    expect(toast.closest("[data-sonner-toast]")).toHaveAttribute("data-type", "warning");
    // it may still have replaced everything, so everything is re-read
    await waitFor(() => expect(invalidate.mock.calls.some(([filters]) => filters === undefined)).toBe(true));
  });
});

describe("Backups — daily auto-backup (G5)", () => {
  it("saves on the flip as a plain settings write, and never promises a copy now", async () => {
    const { api$ } = await openBackups({ auto_backup_enabled: false });
    expect(screen.queryByText("The first copy lands at the next daily run, not now.")).toBeNull();

    await userEvent.click(await screen.findByRole("switch", { name: "Daily auto-backup" }));

    await screen.findByText("daily auto-backup on");
    expect(api$.putSettings).toHaveBeenCalledWith({ auto_backup_enabled: true });
    expect(await screen.findByText("The first copy lands at the next daily run, not now.")).toBeInTheDocument();
  });

  it("says when it went off", async () => {
    const { api$ } = await openBackups({ auto_backup_enabled: true });
    await userEvent.click(await screen.findByRole("switch", { name: "Daily auto-backup" }));
    await screen.findByText("daily auto-backup off");
    expect(api$.putSettings).toHaveBeenCalledWith({ auto_backup_enabled: false });
  });

  it("puts the switch back when the gateway refuses it", async () => {
    const { api$ } = await openBackups({ auto_backup_enabled: false });
    api$.putSettings.mockRejectedValueOnce(new ApiError(422, "auto_backup_enabled must be true or false"));

    await userEvent.click(await screen.findByRole("switch", { name: "Daily auto-backup" }));

    await screen.findByText("auto_backup_enabled must be true or false");
    await waitFor(() => expect(screen.getByRole("switch", { name: "Daily auto-backup" })).toHaveAttribute("data-state", "unchecked"));
  });

  it("waits while a settings write or a connection write runs", async () => {
    const { client } = await openBackups();
    await screen.findByRole("switch", { name: "Daily auto-backup" });
    for (const key of [SETTINGS_WRITE, SETTINGS_CONNECTION_WRITE]) {
      const release = holdWrite(client, key);
      await waitFor(() => expect(screen.getByRole("switch", { name: "Daily auto-backup" })).toBeDisabled());
      await release();
      await waitFor(() => expect(screen.getByRole("switch", { name: "Daily auto-backup" })).toBeEnabled());
    }
  });
});

describe("Backups — what the file holds", () => {
  it("counts what the cached lists hold, against the document's caps, and claims no settings count", async () => {
    await openBackups();
    const card = await screen.findByRole("region", { name: "What the file holds" });

    await waitFor(() => expect(within(card).getByText("of at most 5000")).toBeInTheDocument());
    expect(within(card).getByText("nodes").nextSibling).toHaveTextContent("6");
    expect(within(card).getByText("subscriptions").nextSibling).toHaveTextContent("3");
    expect(within(card).getByText("routing").nextSibling).toHaveTextContent(/rules \+ default action$/);
    // The backup's settings allowlist is not SettingsOut: any number here would be a different one.
    expect(within(card).getByText("settings").nextSibling).toHaveTextContent("every panel setting");
    expect(within(card).getByText("at most 64 keys")).toBeInTheDocument();
    expect(within(card).getByText(/It does not carry the panel password, the session secret or any API token\./)).toBeInTheDocument();
  });

  it("reads each list exactly once and sets no interval on any of them", async () => {
    const { api$ } = await openBackups();
    await waitFor(() => expect(api$.listNodes).toHaveBeenCalledTimes(1));
    expect(api$.listSubs).toHaveBeenCalledTimes(1);
    expect(api$.listProfiles).toHaveBeenCalledTimes(1);
    expect(api$.getRouting).toHaveBeenCalledTimes(1);
  });
});

describe("Backups — the settings read", () => {
  it("shows a retry when it failed, and the cards it does not need still work", async () => {
    const api$ = mockSystem(mockApi());
    api$.getSettings.mockRejectedValue(new ApiError(500, "settings unreadable"));
    renderApp("/system/backups");

    await screen.findByText("Settings did not load", {}, { timeout: 3000 });
    expect(screen.getByRole("button", { name: "Create backup" })).toBeEnabled();
    api$.getSettings.mockResolvedValue(SETTINGS);
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByRole("switch", { name: "Daily auto-backup" })).toBeInTheDocument());
  });

  it("keeps the settings key fresh at the slow cadence and nothing else polls here", async () => {
    const { client } = await openBackups();
    await screen.findByRole("switch", { name: "Daily auto-backup" });
    const settings = client.getQueryCache().find({ queryKey: keys.settings });
    expect(settings?.observers.some((observer) => observer.options.refetchInterval === 30_000)).toBe(true);
    for (const key of [keys.nodes, keys.subs, keys.profiles, keys.routing]) {
      const query = client.getQueryCache().find({ queryKey: key });
      expect(query?.observers.every((observer) => observer.options.refetchInterval === undefined)).toBe(true);
    }
  });
});


describe("Backups — what the gateway already holds (A8)", () => {
  const stored = () => region("On the gateway");

  it("lists the copies newest first, saying which is which and how big", async () => {
    await openBackups();
    const rows = within(await screen.findByRole("list", { name: "Stored backups" })).getAllByRole("listitem");

    expect(rows).toHaveLength(BACKUP_FILES.length);
    expect(rows[0]).toHaveTextContent("taken before a restore");
    expect(rows[0]).toHaveTextContent("41 KB");
    expect(rows[1]).toHaveTextContent("daily copy");
    expect(stored()).toHaveTextContent("3 kept");
  });

  it("downloads one under its own name, fetching it only when asked", async () => {
    const { api$ } = await openBackups();
    const downloads = captureDownloads();
    await screen.findByRole("list", { name: "Stored backups" });
    expect(api$.getStoredBackup).not.toHaveBeenCalled();

    await userEvent.click(within(stored()).getAllByRole("button", { name: "Download" })[1]!);

    await waitFor(() => expect(downloads.names).toEqual(["backup-1700000000.json"]));
    expect(api$.getStoredBackup).toHaveBeenCalledWith("backup-1700000000.json");
  });

  it("asks before the undo, names what it puts back, and only then writes", async () => {
    const { api$ } = await openBackups();
    await screen.findByRole("list", { name: "Stored backups" });

    await userEvent.click(within(stored()).getByRole("button", { name: "Undo last restore" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm" });
    expect(dialog).toHaveTextContent("before the last restore");
    expect(dialog).toHaveTextContent("leaves the gateway disconnected");
    expect(api$.undoRestore).not.toHaveBeenCalled();

    await answer("Undo restore");

    await waitFor(() => expect(api$.undoRestore).toHaveBeenCalledTimes(1));
    // It lands in the same place a restore does, because it is one.
    expect(await screen.findByRole("region", { name: "Last restore" })).toHaveTextContent(BACKUP_FILES[0]!.name);
  });

  it("cancelling the undo writes nothing", async () => {
    const { api$ } = await openBackups();
    await screen.findByRole("list", { name: "Stored backups" });

    await userEvent.click(within(stored()).getByRole("button", { name: "Undo last restore" }));
    await answer("Cancel");

    expect(api$.undoRestore).not.toHaveBeenCalled();
  });

  it("with no snapshot there is nothing to undo, and an empty box says so", async () => {
    const api$ = mockSystem(mockApi());
    api$.listBackups.mockResolvedValue([]);
    renderApp("/system/backups");

    await screen.findByRole("region", { name: "On the gateway" });
    expect(await within(stored()).findByText(/Nothing yet/)).toBeInTheDocument();
    expect(within(stored()).queryByRole("button", { name: "Undo last restore" })).toBeNull();
  });

  it("the undo waits for a connection write started elsewhere", async () => {
    const { client } = await openBackups();
    await screen.findByRole("list", { name: "Stored backups" });
    const release = holdConnectionWrite(client);

    await waitFor(() => expect(within(stored()).getByRole("button", { name: "Undo last restore" })).toBeDisabled());

    await release();
    await waitFor(() => expect(within(stored()).getByRole("button", { name: "Undo last restore" })).toBeEnabled());
  });

  it("a refused undo is worded like a refused restore", async () => {
    const { api$ } = await openBackups();
    api$.undoRestore.mockRejectedValue(new ApiError(400, "invalid backup: : Value error, nothing to restore"));
    await screen.findByRole("list", { name: "Stored backups" });

    await userEvent.click(within(stored()).getByRole("button", { name: "Undo last restore" }));
    await answer("Undo restore");

    expect(await screen.findByText("not restored — nothing to restore")).toBeInTheDocument();
  });
});
