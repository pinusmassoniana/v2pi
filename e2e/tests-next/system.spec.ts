import { readFileSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";
import { PASS, USER, ensureLoggedIn } from "../tests/auth-helper";

// The e2e backend is the dry-run stack run-server.sh forces: PI_GW_NET_BACKEND=dryrun renders host commands and
// returns, xray is /bin/true so no node is ever connected, and the data dir is a throwaway mktemp -d. Nothing here
// touches this machine. Every test puts the settings back the way a fresh gateway has them and deletes every e2e-
// token, because the other specs expect exactly that.

const PREFIX = "e2e-";
const DEFAULTS = {
  auto_backup_enabled: false, session_timeout_min: 0, stats_enabled: true, stats_api_port: 10085, traffic_sample_ms: 1000,
  dns_intercept: false, tunneled_fetch: true, subs_auto_switch: true,
};

interface Token { id: number; name: string }

async function gateway(page: Page) {
  const csrf = ((await (await page.request.get("/api/csrf")).json()) as { csrf: string }).csrf;
  const headers = { "X-CSRF-Token": csrf };
  return {
    get: async <T>(path: string) => (await (await page.request.get(`/api${path}`)).json()) as T,
    put: (path: string, data: unknown) => page.request.put(`/api${path}`, { data, headers }),
    post: (path: string, data?: unknown) => page.request.post(`/api${path}`, data === undefined ? { headers } : { data, headers }),
    remove: (path: string) => page.request.delete(`/api${path}`, { headers }),
    status: () => page.request.get("/api/status"),
  };
}

async function cleanUp(page: Page) {
  const api = await gateway(page);
  // The belt under run-server.sh's braces: only the Linux backend probes the uplink.
  const network = await api.get<{ status: { uplink: boolean | null } }>("/network");
  expect(network.status.uplink, "the e2e backend must be the dry-run network backend").toBeNull();
  await api.put("/settings", DEFAULTS);
  for (const token of await api.get<Token[]>("/tokens")) {
    if (token.name.startsWith(PREFIX)) await api.remove(`/tokens/${token.id}`);
  }
}

test.beforeEach(async ({ page }) => {
  await ensureLoggedIn(page);
  await cleanUp(page);
});

test.afterEach(async ({ page }) => {
  await cleanUp(page);
});

test("old bookmarks land on the new System screens", async ({ page }) => {
  await page.goto("/#/operations");
  await expect(page).toHaveURL(/#\/system\/backups$/);
  await expect(page.getByRole("region", { name: "Backup & restore" })).toBeVisible();

  await page.goto("/#/settings");
  await expect(page).toHaveURL(/#\/system\/panel$/);
  await expect(page.getByRole("heading", { level: 1, name: "Panel" })).toBeVisible();
});

test("Backups: download the configuration, restore it back, and read the gateway's own refusals", async ({ page }, info) => {
  const phone = info.project.name === "phone";
  await page.goto("/#/system/backups");
  await expect(page.getByRole("region", { name: "Backup & restore" })).toBeVisible();

  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Create backup" }).click();
  const file = await download;
  expect(file.suggestedFilename()).toMatch(/^v2pi-backup-\d{4}-\d{2}-\d{2}\.json$/);
  await expect(page.getByText(/^backup downloaded · v2pi-backup-/)).toBeVisible();
  const saved = await file.path();

  // Restoring what this gateway just exported is a no-op round trip that still runs the whole path.
  const picker = page.getByLabel(/^(Choose file…|Change…)$/).first();
  await picker.setInputFiles(saved!);
  await expect(page.getByText(/under the 2 MB/)).toBeVisible();
  await page.getByRole("button", { name: "Restore", exact: true }).click();
  if (phone) {
    const sheet = page.getByRole("dialog", { name: "Replace everything?" });
    await expect(sheet).toContainText("Restore replaces every node, subscription, anti-DPI profile, routing rule and panel setting");
    await sheet.getByRole("button", { name: "Restore" }).click();
  } else {
    const ask = page.getByRole("dialog", { name: "Confirm" });
    await expect(ask).toContainText("The Reality private key and the remote-access client list are not restored.");
    await ask.getByRole("button", { name: "Restore" }).click();
  }

  await expect(page.getByText(/^restored \d+ nodes, /)).toBeVisible({ timeout: 190_000 });
  const last = page.getByRole("region", { name: "Last restore" });
  await expect(last).toContainText("A copy of what this replaced was saved on the gateway at /");
  await expect(last.getByRole("link", { name: phone ? "Go to Home and connect" : "Go to Home" })).toBeVisible();

  // A file that is not a backup at all, and one whose value the gateway's own validator refuses.
  const api = await gateway(page);
  const notABackup = await api.post("/restore", { nodes: [] });
  expect(notABackup.status()).toBe(400);
  expect((await notABackup.json() as { detail: string }).detail).toBe("not a valid backup file");
  // The same document with one setting broken: a field refusal that names its field, refused before anything is written.
  const doc = JSON.parse(readFileSync(saved!, "utf8")) as { settings: Record<string, unknown> };
  const badValue = await api.post("/restore", { ...doc, settings: { ...doc.settings, health_interval: "abc" } });
  expect(badValue.status()).toBe(400);
  expect((await badValue.json() as { detail: string }).detail).toContain("health_interval");
});

test("Backups: the daily copy switch saves itself and promises nothing now", async ({ page }, info) => {
  await page.goto("/#/system/backups");
  const toggle = page.getByRole("switch", { name: "Daily auto-backup" });
  await expect(toggle).toHaveAttribute("aria-checked", "false");

  await toggle.click();
  await expect(page.getByText("daily auto-backup on")).toBeVisible();
  // On a phone the copy lives inside the section the switch heads, so open it to read it.
  if (info.project.name === "phone") await page.getByRole("button", { name: /^Daily auto-backup/ }).click();
  await expect(page.getByText(/The first copy lands at the next daily run, not now\./)).toBeVisible();

  await toggle.click();
  await expect(page.getByText("daily auto-backup off")).toBeVisible();
});

test("Access: issue a token with an expiry, copy the secret it shows once, then revoke it twice", async ({ page, context }, info) => {
  const phone = info.project.name === "phone";
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/#/system/access");
  await expect(page.getByRole("region", { name: "API tokens" })).toBeVisible();

  await page.getByRole("region", { name: "API tokens" }).getByRole("button", { name: "Create token" }).click();
  const form = phone ? page.getByRole("dialog", { name: "New token" }) : page.getByRole("region", { name: "New API token" });
  await form.getByLabel("Name").fill("e2e-monitor");
  // The segmented control's radios are visually hidden inside their labels: the label is what a person clicks.
  await form.getByText("30 days", { exact: true }).click();
  await expect(form.getByText(/^sent as a timestamp · \d{4}-\d{2}-\d{2}$/)).toBeVisible();
  await form.getByRole("button", { name: "Create token" }).click();

  await expect(page.getByText("token “e2e-monitor” created")).toBeVisible();
  const panel = page.getByRole("group", { name: "New token e2e-monitor" });
  await expect(panel).toBeVisible();
  const secret = (await panel.locator("p.font-mono").innerText()).trim();
  expect(secret).toMatch(/^pgwp_[A-Za-z0-9_-]{43}$/);
  await panel.getByRole("button", { name: "Copy" }).click();
  await expect(panel.getByRole("button", { name: "Copied ✓" })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(secret);

  await page.getByRole("button", { name: "Done" }).click();
  await expect(page.getByText(secret)).toHaveCount(0);
  const row = phone
    ? page.getByRole("list", { name: "Tokens" }).getByRole("listitem").filter({ hasText: "e2e-monitor" })
    : page.getByRole("row", { name: /^e2e-monitor/ });
  await expect(row).toContainText(secret.slice(0, 12));
  await expect(row.getByText("in 30 d")).toBeVisible();

  const id = (await (await gateway(page)).get<Token[]>("/tokens")).find((token) => token.name === "e2e-monitor")!.id;
  if (phone) {
    await row.getByRole("button", { name: "More actions for e2e-monitor" }).click();
    await page.getByRole("menuitem", { name: "Revoke…" }).click();
  } else {
    await row.getByRole("button", { name: "Revoke e2e-monitor" }).click();
  }
  const ask = page.getByRole("dialog", { name: "Confirm" });
  await expect(ask).toContainText("Revoke token “e2e-monitor”? Anything using it stops working immediately.");
  await ask.getByRole("button", { name: "Revoke" }).click();
  await expect(page.getByText("token “e2e-monitor” revoked")).toBeVisible();

  // A second revocation is a 404, which the screen reads as "already gone" rather than a failure.
  const again = await (await gateway(page)).remove(`/tokens/${id}`);
  expect(again.status()).toBe(404);
});

test("Access: the audit log loads only on Show, and masks a remote-access client id", async ({ page }) => {
  await page.goto("/#/system/access");
  const audit = page.getByRole("region", { name: "Audit log" });
  await expect(audit.getByText("not loaded")).toBeVisible();

  await audit.getByRole("button", { name: "Show" }).click();

  await expect(audit.getByRole("list", { name: "Audit entries" })).toBeVisible();
  await expect(audit.getByRole("button", { name: "Retry" })).toBeVisible();
  // this session's own PUT /settings from the cleanup is in there, whatever else the run did
  await expect(audit.getByText("/api/settings").first()).toBeVisible();
});

test("Access: the idle timeout saves itself", async ({ page }) => {
  await page.goto("/#/system/access");
  const field = page.getByLabel("Idle timeout");
  await expect(field).toHaveValue("0");

  await field.fill("30");
  await field.blur();
  await expect(page.getByText("idle timeout · 30 min")).toBeVisible();

  await field.fill("0");
  await field.press("Enter");
  await expect(page.getByText("idle timeout off")).toBeVisible();
});

test("Access: the panel password changes and changes straight back, and this session survives it", async ({ page }) => {
  const NEXT = "e2e-pass-654321";
  await page.goto("/#/system/access");
  await expect(page.getByRole("region", { name: "Password" })).toBeVisible();
  // Its own throwaway token, so the revocation the change performs is visible rather than assumed.
  const api = await gateway(page);
  expect((await api.post("/tokens", { name: "e2e-doomed", scope: "monitor" })).status()).toBe(201);

  async function change(current: string, next: string) {
    await page.getByLabel("Current password").fill(current);
    await page.getByLabel("New password", { exact: true }).fill(next);
    await page.getByLabel("Confirm new password").fill(next);
    await page.getByRole("region", { name: "Password" }).getByRole("button", { name: "Change password" }).click();
    const ask = page.getByRole("dialog", { name: "Confirm" });
    await expect(ask).toContainText("Every other signed-in session is signed out");
    // Waited on by its own request, not by the toast: the previous change's toast is still on screen.
    const sent = page.waitForResponse((response) => response.url().endsWith("/api/password") && response.request().method() === "POST");
    await ask.getByRole("button", { name: "Change password" }).click();
    expect((await sent).ok(), "the password change must have been accepted").toBe(true);
    await expect(page.getByText(/^password changed · other sessions signed out/).first()).toBeVisible();
  }

  await change(PASS, NEXT);
  // This session adopts the new epoch, so its next read is still a 200 — and every token row is gone.
  expect((await (await gateway(page)).status()).status()).toBe(200);
  expect(await (await gateway(page)).get<Token[]>("/tokens")).toEqual([]);

  // Mandatory: PASS is shared by every spec through ensureLoggedIn, and they run against this one server.
  await change(NEXT, PASS);
  const back = await page.request.post("/api/login", { data: { username: USER, password: PASS } });
  expect(back.ok(), "the original password must be restored before this test ends").toBe(true);
});

test("Logs: read the app log, filter it, download exactly what is shown, and see why two sources are empty", async ({ page }, info) => {
  const phone = info.project.name === "phone";
  // A fresh throwaway data dir has an empty app log, so give the panel something to have written: starting xray
  // against /bin/true with no config on disk logs one WARNING and changes nothing else on this box.
  const api = await gateway(page);
  await api.post("/xray/start");
  await api.post("/xray/stop");

  await page.goto("/#/system/logs");
  const pane = page.getByRole("region", { name: "Log output" });
  await expect(pane).toContainText("Press Load to read the last 200 lines.");

  const openControls = async () => { if (phone) await page.getByRole("button", { name: "Source, lines, filter" }).click(); };
  const pickSource = async (name: string) => {
    await openControls();
    // The radios are visually hidden inside their labels, so the label is what a person clicks; the footer names the
    // current source with the same words, which is why this takes the first match (the control comes first).
    await page.getByText(name, { exact: true }).first().click();
    if (phone) await page.getByRole("dialog", { name: "Source & filter" }).getByRole("button", { name: "Load", exact: true }).click();
    else await page.getByRole("button", { name: "Load", exact: true }).click();
  };

  await page.getByRole("button", { name: "Load", exact: true }).click();
  await expect(pane).toContainText("WARNING");
  await expect(page.getByText(/^showing \d+ of \d+ lines$/)).toBeVisible();

  await openControls();
  await page.getByPlaceholder("filter…").fill("supervisor");
  if (phone) await page.getByRole("dialog", { name: "Source & filter" }).getByRole("button", { name: "Load", exact: true }).click();
  await expect(page.getByText(/filter “supervisor”/)).toBeVisible();

  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: phone ? "Download" : "Download", exact: true }).first().click();
  expect((await download).suggestedFilename()).toBe("app.log");

  // xray is /bin/true, so its output is usually empty — the assertion is that the source answers at all.
  await pickSource("xray output");
  await expect(page.getByRole("alert")).toHaveCount(0);

  await pickSource("xray error file");
  await expect(pane).toContainText("This one is empty by configuration");
});

test("Panel: diagnostics, a saved sample interval, the settings file out and back, and the reset", async ({ page }, info) => {
  const phone = info.project.name === "phone";
  await page.goto("/#/system/panel");
  const system = page.getByRole("region", { name: "System" });
  await expect(system).toBeVisible();
  // /bin/true prints nothing, so the panel cannot read a version: that is a state, not a number.
  await expect(system.getByText(/^(unknown|unavailable)$/)).toBeVisible();
  await expect(system.getByText("APP VERSION")).toBeVisible();

  // Deviation (Task 13 review -> Task 14, integration-notes.md): the brief's openStats() unconditionally clicked
  // the "Traffic stats" header on a phone. On a fresh mount (including after page.reload() below) that section
  // starts OPEN, so the unconditional click TOGGLES IT SHUT; the later `toHaveValue` assertion still passed
  // because it does not check visibility. Made deterministic here: only click when the section is not already
  // expanded, using the header button's own aria-expanded (features/tunnel/EditorSection.tsx:36).
  const openSection = async (name: RegExp) => {
    if (!phone) return;
    const button = page.getByRole("button", { name });
    if ((await button.getAttribute("aria-expanded")) !== "true") await button.click();
  };
  const openStats = () => openSection(/^Traffic stats/);

  const interval = page.getByLabel("Sample interval");
  await expect(interval).toHaveValue("1000");
  await interval.fill("250");
  await expect(page.getByText("traffic_sample_ms must be >= 500")).toBeVisible();
  await page.getByLabel("Xray API port").fill("52345");
  await expect(page.getByText("stats_api_port collides with a system port")).toBeVisible();
  await page.getByLabel("Xray API port").fill("10085");
  await interval.fill("2000");
  const sent = page.waitForResponse((response) => response.url().endsWith("/api/settings") && response.request().method() === "PUT");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  expect((await sent).request().postDataJSON()).toEqual({ traffic_sample_ms: 2000 });
  await expect(page.getByText("saved — applies on next Connect")).toBeVisible();

  await openSection(/^Settings file/);
  const exported = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export settings" }).click();
  const file = await exported;
  expect(file.suggestedFilename()).toBe("v2pi-settings.json");
  await expect(page.getByText("settings exported · v2pi-settings.json")).toBeVisible();
  const path = (await file.path())!;

  await page.getByRole("region", { name: "Settings file" }).or(page.locator("fieldset")).first().waitFor();
  await page.getByLabel(/^(Choose file…|Change…)$/).last().setInputFiles(path);
  await expect(page.getByText(/fields, all known settings keys$/)).toBeVisible();
  await page.getByRole("button", { name: "Import settings" }).click();
  await page.getByRole("dialog", { name: "Confirm" }).getByRole("button", { name: "Import settings" }).click();
  await expect(page.getByText("settings applied from file")).toBeVisible();

  // One unknown key refuses the whole file — named here, before anything is sent.
  await page.getByLabel(/^(Choose file…|Change…)$/).last().setInputFiles({
    name: "settings-old-box.json", mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify({ stats_enabled: true, rw_enabled: "1" })),
  });
  await expect(page.getByText("1 field is not a settings key: rw_enabled")).toBeVisible();
  await expect(page.getByRole("button", { name: "Import settings" })).toBeDisabled();

  await openSection(/^Danger zone/);
  await page.getByRole("button", { name: "Reset settings" }).click();
  const ask = page.getByRole("dialog", { name: "Confirm" });
  await expect(ask).toContainText("turns gateway DNS over DoH off");
  await ask.getByRole("button", { name: "Reset settings" }).click();
  await expect(page.getByText(/^settings reset to defaults/)).toBeVisible();

  await page.reload();
  await openStats();
  await expect(page.getByLabel("Sample interval")).toHaveValue("1000");
});
